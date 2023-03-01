from torch import nn
import torch
import torch.nn.functional as F
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange, Reduce

# Residual Block
class ResidualAdd(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x):
        res = x
        x = self.fn(x)
        x += res
        
        return x

# Attention 
class MultiHeadAttention(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.emb_size = args_dict['emb_size']
        self.num_heads = args_dict['num_heads']
        self.qkv = nn.Linear(self.emb_size, self.emb_size * 3)
        self.att_drop = nn.Dropout(args_dict['drop_p'])
        self.projection = nn.Linear(self.emb_size, self.emb_size)
        
    def forward(self, x, mask=None):

        # q = rearrange(self.qkv(x), "b n (h d) -> b h n d", h=self.num_heads)
        # k = rearrange(self.qkv(x), "b n (h d) -> b h n d", h=self.num_heads)
        # v = rearrange(self.qkv(x), "b n (h d) -> b h n d", h=self.num_heads)
        
        qkv = rearrange(self.qkv(x), "b n (h d qkv) -> (qkv) b h n d", h=self.num_heads, qkv=3)
        q, k, v = qkv[0], qkv[1], qkv[2]
     
        energy = torch.einsum('bhqd, bhkd -> bhqk', q, k) # batch, num_heads, query_len, key_len
        if mask is not None:
            fill_value = torch.finfo(torch.float32).min
            energy.mask_fill(~mask, fill_value)
            
        scaling = self.emb_size ** (1/2)
        att = F.softmax(energy, dim=-1) / scaling
        att = self.att_drop(att)

        out = torch.einsum('bhal, bhlv -> bhav ', att, v)
        out = rearrange(out, "b h n d -> b n (h d)")
        out = self.projection(out)
        return out

# MLP
class FeedForward(nn.Module):
    def __init__(self, emb_size, expansion, drop_p=0):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(emb_size, expansion * emb_size),
            nn.GELU(),
            nn.Dropout(drop_p),
            nn.Linear(expansion * emb_size, emb_size),
            nn.Dropout(drop_p),
        )
    def forward(self, x):
        return self.net(x)

# Patch + Position Embedding
class PatchEmbeddings(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.patch_size = args_dict['patch_size']
        self.in_channels = args_dict['in_channels']
        self.emb_size = args_dict['emb_size']

        
        self.cls_token = nn.Parameter(torch.randn(1, 1, self.emb_size))
        
        self.proj = nn.Sequential(
            # Rearrange('b c (h s1) (w s2) -> b (h w) (s1 s2 c)', s1=self.patch_size, s2=self.patch_size),
            # nn.Linear(self.patch_size * self.patch_size * self.in_channels, self.emb_size)
            nn.Conv2d(self.in_channels, self.emb_size, kernel_size=self.patch_size, stride=self.patch_size), #  1,768,14,14
            Rearrange('b e (h) (w) -> b (h w) e'), # 1, 196, 768
        )

        # position embedding
        # self.positions = nn.Parameter(torch.randn((args_dict['img_size'] // args_dict['emb_size']) **2 + 1, args_dict['emb_size']))
    
    def forward(self, x):
        b, _, _, _ = x.shape
        x = self.proj(x)

        cls_token = repeat(self.cls_token, '() n e -> b n e', b=b)
        x = torch.cat([cls_token, x], dim=1)

        # position embedding
        # x += self.positions
        
        return x

class Reconstruction(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.patch_size = args_dict['patch_size']
        self.in_channels = args_dict['in_channels']
        self.emb_size = args_dict['emb_size']

        self.proj = nn.Sequential(
            Rearrange('b (h w) e -> b e (h) (w)', h=14),
            nn.ConvTranspose2d(self.emb_size, self.in_channels, kernel_size=self.patch_size, stride=self.patch_size)
        )

    def forward(self, x):
        x = x[:, 1:, :]
        return self.proj(x)
    

class PositionEmbeddings(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.positions = nn.Parameter(torch.randn((args_dict['img_size'] // args_dict['emb_size']) **2 + 1, args_dict['emb_size']))

    def forward(self, x):
        x += self.positions
        
        return x

### LayerNorm -> MultiHead -> LayerNorm -> MLP ->  
###     |                  ^      |             ^
###     --------------------      ---------------

class TransformerEncoderBlock(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.encoderblock = nn.Sequential(
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(args_dict['emb_size']),
                MultiHeadAttention(args_dict),
                nn.Dropout(args_dict['drop_p'])
            )),
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(args_dict['emb_size']),
                FeedForward(args_dict['emb_size'], args_dict['expansion'], args_dict['drop_p']),
                nn.Dropout(args_dict['drop_p'])
            ))
        )
    def forward(self, x):
        return self.encoderblock(x) 

class TransformerEncoder(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
 
        self.TransformerEncoder = nn.Sequential(
            *[TransformerEncoderBlock(args_dict) for _ in range(args_dict['depth'])]
        )

    def forward(self, x):

        x = self.TransformerEncoder(x)
        return x
    
class TransformerDecoderBlock(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.decoderblock = nn.Sequential(
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(args_dict['decoder_embed_dim']),
                MultiHeadAttention(args_dict),
                nn.Dropout(args_dict['drop_p'])
            )),
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(args_dict['decoder_embed_dim']),
                FeedForward(args_dict['decoder_embed_dim'], args_dict['expansion'], args_dict['drop_p']),
                nn.Dropout(args_dict['drop_p'])
            ))
        )
    def forward(self, x):
        return self.decoderblock(x)

class TransformerDecoder(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.deocder_embed = nn.Linear(args_dict['emb_size'], args_dict['decoder_embed_dim'], bias=True)
        self.embed = PatchEmbeddings(args_dict)
        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, args_dict['patch_size']+1))
        self.TransformerDecoder = nn.Sequential(
            *[TransformerDecoderBlock(args_dict) for _ in range(args_dict['decoder_depth'])]
        )
        self.decoderNorm = nn.LayerNorm(args_dict['decoder_embed_dim'])
        self.decoder_pred = nn.Linear(args_dict['decoder_embed_dim'], args_dict['patch_size']**2 * args_dict['in_channels'], bias=True) # decoder to patch


    def forward(self, x):
        x = self.deocder_embed(x)
        x = self.TransformerDecoder(x)
        x = self.decoderNorm(x)
        x = self.decoder_pred(x)

        return x



class ClassificationHead(nn.Module):
    def __init__(self, args_dict):
        super().__init__()
        self.ClassificationHead = nn.Sequential(
            Reduce('b n e -> b e', reduction='mean'),
            nn.LayerNorm(args_dict['emb_size']),
            nn.Linear(args_dict['emb_size'], args_dict['n_classes'])
        )
    
    def forward(self, x):
        return self.ClassificationHead(x)

