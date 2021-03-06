import torch
import torch.nn as nn
import torch.nn.functional as F

# 为了解决import出错
import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)+'/'+'..'))

# 使用绝对路径引入自己的包
from Derivable_Models.Gan_Utils import get_gan_model


PGGAN_LATENT_1024 = [(512, 1, 1),
              (512, 4, 4), (512, 4, 4),
              (512, 8, 8), (512, 8, 8),
              (512, 16, 16), (512, 16, 16),
              (512, 32, 32), (512, 32, 32),
              (256, 64, 64), (256, 64, 64),
              (128, 128, 128), (128, 128, 128),
              (64, 256, 256), (64, 256, 256),
              (32, 512, 512), (32, 512, 512),
              (16, 1024, 1024), (16, 1024, 1024),
              (3, 1024, 1024)]

PGGAN_LATENT_256 = [(512, 1, 1), (512, 4, 4),
              (512, 4, 4), (512, 8, 8),
              (512, 8, 8), (512, 16, 16),
              (512, 16, 16), (512, 32, 32),
              (512, 32, 32), (256, 64, 64),
              (256, 64, 64), (128, 128, 128),
              (128, 128, 128), (64, 256, 256),
              (64, 256, 256), (3, 256, 256)]

PGGAN_LAYER_MAPPING = {  # The new PGGAN includes the intermediate output layer, need mapping
    0: 0, 1: 1, 2: 3, 3: 4, 4: 6, 5: 7, 6: 9, 7: 10, 8: 12
}

# 选择生成器类型
def get_derivable_generator(gan_model_name, generator_type, args):
    if generator_type == 'PGGAN-z':  # Single latent code
        return PGGAN(gan_model_name)
    elif generator_type == 'PGGAN-Multi-Z':  # Multiple Latent Codes            # 默认使用此类型
        return PGGAN_multi_z(gan_model_name, args.composing_layer, args.z_number, args)
    else:
        raise Exception('Please indicate valid `generator_type`')


class PGGAN(nn.Module):
    def __init__(self, gan_model_name):
        super(PGGAN, self).__init__()
        self.pggan = get_gan_model(gan_model_name)
        self.init = False

    def input_size(self):
        return [(512,)]

    def cuda(self, device=None):
        self.pggan.cuda(device=device)

    def forward(self, z):
        latent = z[0]
        return self.pggan(latent)

# 默认类型
# PGGAN_multi_z(gan_model_name, args.composing_layer, args.z_number, args)
class PGGAN_multi_z(nn.Module):
    def __init__(self, gan_model_name, blending_layer, z_number, args):
        super(PGGAN_multi_z, self).__init__()
        self.blending_layer = blending_layer        # default = 6
        self.z_number = z_number        # latent codes的数量. default=30
        self.z_dim = 512
        self.pggan = get_gan_model(gan_model_name)    

        # generator被分成了两个子网络
        # blending_layer即为论文中intermediate layer(中间层)
        # 这样的划分是为了从任意给定的Zn中提取出相应的空间特征用于进一步的合成
        self.pre_model = nn.Sequential(*list(self.pggan.children())[:blending_layer])
        self.post_model = nn.Sequential(*list(self.pggan.children())[blending_layer:])
        self.init = True


        PGGAN_LATENT = PGGAN_LATENT_1024 if gan_model_name == 'PGGAN-CelebA' else PGGAN_LATENT_256
        self.mask_size = PGGAN_LATENT[blending_layer][1:]
        self.layer_c_number = PGGAN_LATENT[blending_layer][0]

    def input_size(self):   # 接收的参数矩阵格式
        return [(self.z_number, self.z_dim), (self.z_number, self.layer_c_number)]

    def init_value(self, batch_size):
        # 随机生成预计值
        z_estimate = torch.randn((batch_size, self.z_number, self.z_dim)).cuda()  # our estimate, initialized randomly
        # torch.full(size, fill_value, out=None, dtype=None, layout=torch.strided, device=None, requires_grad=False) → Tensor
        z_alpha = torch.full((batch_size, self.z_number, self.layer_c_number), 1 / self.z_number).cuda()    # 全部用 1/z_number填充
        # z_alpha即为adaptive channel importance, 对于每一个Zn帮助他们适应不同的语义
        # z_alpha的每一个元素代表了feature map对应的channel的重要性
        return [z_estimate, z_alpha]

    def cuda(self, device=None):
        self.pggan.cuda(device=device)

    def forward(self, z):
        z_estimate, alpha_estimate = z
        feature_maps_list = []
        for j in range(self.z_number):
            feature_maps_list.append(       # 从随机预计值生成feature maps并存入list
                # torch矩阵 A * B 运算为逐位相乘, 对应论文第3页公式(2). 
                # 使用alpha_estimate对feature map进行加权
                self.pre_model(z_estimate[:, j, :].view((-1, self.z_dim, 1, 1))) * alpha_estimate[:, j, :].view((-1, self.layer_c_number, 1, 1)))
        # 每组latent code对应生成一个feature map, 使用多组latent codes来生成feature maps并融合结果
        fused_feature_map = sum(feature_maps_list) / self.z_number      # 求所有feature maps的均值(feature maps按位求和再除以latent codes的数量)
        y_estimate = self.post_model(fused_feature_map)     # 从feature maps生成神经网络预计的图像(此时为tesnor, 需要转为image)
        return y_estimate

