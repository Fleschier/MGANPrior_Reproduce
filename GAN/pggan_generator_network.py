# python 3.7
"""Contains the implementation of generator described in PGGAN.

Different from the official tensorflow version in folder `pggan_tf_official`,
this is a simple pytorch version which only contains the generator part. This
class is specially used for inference.

For more details, please check the original paper:
https://arxiv.org/pdf/1710.10196.pdf
"""

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

# 开放接口
__all__ = ['PGGANGeneratorNet']

# Resolutions allowed.
_RESOLUTIONS_ALLOWED = [8, 16, 32, 64, 128, 256, 512, 1024]

# Initial resolution.
_INIT_RES = 4


class PGGANGeneratorNet(nn.Module):
  """Defines the generator network in PGGAN.

  NOTE: The generated images are with `RGB` color channels and range [-1, 1].
  """

  def __init__(self,
               resolution=1024,
               z_space_dim=512,
               image_channels=3,
               fused_scale=False,
              # 层与层之间会有若干个卷积核（kernel），上一层中的每个feature map(特征图)跟每个卷积核做卷积，
              # 对应产生下一层的一个feature map。
               fmaps_base=16 << 10,   # 2^14   .写成左移10位的目的:  当后面循环中res > 2^5时, fmaps_base // res 就会小于 fmaps_max
               fmaps_max=512):        # 设置为z_space_dim一样的值, 2^9
    """Initializes the generator with basic settings.

    Args:
      resolution: The resolution of the output image. (default: 1024)
      z_space_dim: The dimension of the initial latent space. (default: 512)
      image_channels: Number of channels of the output image. (default: 3)
      fused_scale: Whether to fused `upsample` and `conv2d` together, resulting
        in `conv2d_transpose`. (default: False)
      fmaps_base: Base factor to compute number of feature maps for each layer.
        (default: 16 << 10)   
      fmaps_max: Maximum number of feature maps in each layer. (default: 512)

    Raises:
      ValueError: If the input `resolution` is not supported.
    """
    super().__init__()

    if resolution not in _RESOLUTIONS_ALLOWED:
      raise ValueError(f'Invalid resolution: {resolution}!\n'
                       f'Resolutions allowed: {_RESOLUTIONS_ALLOWED}.')

    self.init_res = _INIT_RES   # 4
    self.init_res_log2 = int(np.log2(self.init_res))
    self.resolution = resolution  #　 1024
    self.final_res_log2 = int(np.log2(self.resolution))     ### 网络每经过一次卷积块，分辨率扩大一倍， 故据此计算循环次数

    self.z_space_dim = z_space_dim
    self.image_channels = image_channels
    self.fused_scale = fused_scale
    self.fmaps_base = fmaps_base
    self.fmaps_max = fmaps_max

    ###  做什么用的?
    self.num_layers = (self.final_res_log2 - self.init_res_log2 + 1) * 2

    # torch.nn.Parameter(torch.FloatTensor(hidden_size))
    # 将一个固定不可训练的tensor转换成可以训练的类型parameter，并将这个parameter绑定到这个module里面所以经过类型转换
    # 这个self.v变成了模型的一部分，成为了模型中根据训练可以改动的参数了。
    # 使用这个函数的目的也是想让某些变量在学习的过程中不断的修改其值以达到最优化。

    # 使用nn.Parameter()来转换一个固定的权重数值，使的其可以跟着网络训练一直调优下去，学习到一个最适合的权重值。
    self.lod = nn.Parameter(torch.zeros(()))  # tensor(0., requires_grad=True)
    self.pth_to_tf_var_mapping = {'lod': 'lod'}   # 保存每一层网络的权重和偏置的路径

    for res_log2 in range(self.init_res_log2, self.final_res_log2 + 1):   # 范围 [2, 10]
      res = 2 ** res_log2 # 乘方      res的范围: 4 ~ 1024, 为2的指数倍
      block_idx = res_log2 - self.init_res_log2   # [0,8]

      # First convolution layer for each resolution.
      """
      卷积块(含多层神经网络)
      或者理解为,卷积层分为三级: 卷积级, 探测级(使用激励函数非线性化), 池化级
      """
      if res == self.init_res:          # 接收输入的第一层卷积层

        self.add_module( 
          # 输入参数为Module.add_module(name: str, module: Module)。
          # 功能为，为Module添加一个子module，对应名字为name
          # 用torch.nn.Sequential()容器进行快速搭建时，模型的各层被顺序添加到容器中。缺点是每层的编号是默认的阿拉伯数字，不易区分
          # 通过add_module()添加每一层，并且为每一层增加了一个单独的名字。 
            f'layer{2 * block_idx}',    # block_idx [0, 8]
            # 卷积核的输入通道数（in depth）由输入矩阵的通道数(z_space_dim)所决定
            # 输出矩阵的通道数（out depth）由卷积核的输出通道数所决定(即卷积核或者feature maps的个数)
            ConvBlock(in_channels=self.z_space_dim,   # 由于是第一层,故输入的通道数为初始输入矩阵的z_space_dim
                      out_channels=self.get_nf(res),  # 输出的通道数, 
                      kernel_size=self.init_res,    # 卷积核的尺寸设为初始尺寸=4, 默认kernel size为3
                      padding=3))   # padding=3, 默认stride为1, 默认dilation rate=1   则输出的宽度会变为 n+3
        # 路径存入字典
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.conv.weight'] = (
            f'{res}x{res}/Dense/weight')
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.wscale.bias'] = (
            f'{res}x{res}/Dense/bias')
      else:   # 如果不是第一层
        self.add_module(
            f'layer{2 * block_idx}',
            ConvBlock(in_channels=self.get_nf(res // 2),    # 输入
                      out_channels=self.get_nf(res),
                      upsample=True,    # 需要上采样
                      fused_scale=self.fused_scale))
        if self.fused_scale:    # 是否用upsample+con2v
          self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.weight'] = (
              f'{res}x{res}/Conv0_up/weight')
          self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.wscale.bias'] = (
              f'{res}x{res}/Conv0_up/bias')
        else:
          self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.conv.weight'] = (
              f'{res}x{res}/Conv0/weight')
          self.pth_to_tf_var_mapping[f'layer{2 * block_idx}.wscale.bias'] = (
              f'{res}x{res}/Conv0/bias')

      # Second convolution layer for each resolution.
      self.add_module(
          f'layer{2 * block_idx + 1}',
          ConvBlock(in_channels=self.get_nf(res),
                    out_channels=self.get_nf(res)))
      if res == self.init_res:
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx + 1}.conv.weight'] = (
            f'{res}x{res}/Conv/weight')
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx + 1}.wscale.bias'] = (
            f'{res}x{res}/Conv/bias')
      else:
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx + 1}.conv.weight'] = (
            f'{res}x{res}/Conv1/weight')
        self.pth_to_tf_var_mapping[f'layer{2 * block_idx + 1}.wscale.bias'] = (
            f'{res}x{res}/Conv1/bias')

      # Output convolution layer for each resolution.
      self.add_module(
          f'output{block_idx}',
          ConvBlock(in_channels=self.get_nf(res),
                    out_channels=self.image_channels,   # 输出层为RGB图像,故通道数为image_channels,一般为3
                    kernel_size=1,    # 使用size=1的kernel达到将z_dim快速降下来并且不损失图像原有尺寸精度的效果
                    padding=0,
                    wscale_gain=1.0,
                    activation_type='linear'))
      self.pth_to_tf_var_mapping[f'output{block_idx}.conv.weight'] = (
          f'ToRGB_lod{self.final_res_log2 - res_log2}/weight')
      self.pth_to_tf_var_mapping[f'output{block_idx}.wscale.bias'] = (
          f'ToRGB_lod{self.final_res_log2 - res_log2}/bias')
    
    # 上采样方法
    self.upsample = ResolutionScalingLayer()

  # 通过当前resolution计算feature maps的数量,作为输入/输出矩阵的通道数
  ###  原理待解决
  def get_nf(self, res):
    """Gets number of feature maps according to current resolution."""
    return min(self.fmaps_base // res, self.fmaps_max)

  # 搭建网络
  def forward(self, z):
    if not (len(z.shape) == 2 and z.shape[1] == self.z_space_dim):
      raise ValueError(f'The input tensor should be with shape [batch_size, '
                       f'latent_space_dim], where `latent_space_dim` equals to '
                       f'{self.z_space_dim}!\n'
                       f'But {z.shape} received!')

    # view()方法使得x和z共享相同的底层数据,改动x也会影响z
    # z就是一个 batch_size * z_space_dim的张量
    # x只是z的另一个形状的表达, x为 z.shap[0] * z_space_dim * 1 * 1的tensor
    x = z.view(z.shape[0], self.z_space_dim, 1, 1)

    lod = self.lod.cpu().tolist() # 将训练的权值加载到CPU以便操作,此时lod是一个数值

    for res_log2 in range(self.init_res_log2, self.final_res_log2 + 1):
      # 小于权重阈值的放入神经网络训练
      if res_log2 + lod <= self.final_res_log2:
        block_idx = res_log2 - self.init_res_log2
        x = self.__getattr__(f'layer{2 * block_idx}')(x)    # 将x放入神经网络训练
        x = self.__getattr__(f'layer{2 * block_idx + 1}')(x)
        image = self.__getattr__(f'output{block_idx}')(x)
      else:
        # 大于权重阈值的进行上采样提高分辨率
        image = self.upsample(image)

    
    return image

# 像素特征向量归一化层
class PixelNormLayer(nn.Module):
  """Implements pixel-wise feature vector normalization layer."""

  def __init__(self, epsilon=1e-8):
    super().__init__()
    self.eps = epsilon

  def forward(self, x):
    return x / torch.sqrt(torch.mean(x ** 2, dim=1, keepdim=True) + self.eps)


# 该层用于通过最近邻插值从空间域对feature maps进行上采样, 从而实现提高图像分辨率
class ResolutionScalingLayer(nn.Module):
  """Implements the resolution scaling layer.

  Basically, this layer can be used to upsample feature maps from spatial domain
  with nearest neighbor interpolation.
  """

  def __init__(self, scale_factor=2):
    super().__init__()
    self.scale_factor = scale_factor

  def forward(self, x):
    # 上下采样方法interpolate
    # torch.nn.functional.interpolate(input, size=None, scale_factor=None, mode='nearest', align_corners=None)
    # input (Tensor) – 输入张量
    # size (int or Tuple[int] or Tuple[int, int] or Tuple[int, int, int]) – 输出大小.
    # scale_factor (float or Tuple[float]) – 指定输出为输入的多少倍数。如果输入为tuple，其也要制定为tuple类型
    return F.interpolate(x, scale_factor=self.scale_factor, mode='nearest')


class WScaleLayer(nn.Module):
  """Implements the layer to scale weight variable and add bias.

  NOTE: The weight variable is trained in `nn.Conv2d` layer, and only scaled
  with a constant number, which is not trainable in this layer. However, the
  bias variable is trainable in this layer.
  """

  def __init__(self, in_channels, out_channels, kernel_size, gain=np.sqrt(2.0)):
    super().__init__()
    fan_in = in_channels * kernel_size * kernel_size
    self.scale = gain / np.sqrt(fan_in)
    self.bias = nn.Parameter(torch.zeros(out_channels))

  def forward(self, x):
    return x * self.scale + self.bias.view(1, -1, 1, 1)

# 依次调用多层神经网络,构成一个神经网络块
class ConvBlock(nn.Module):
  """Implements the convolutional block.

  Basically, this block executes pixel-wise normalization layer, upsampling
  layer (if needed), convolutional layer, weight-scale layer, and activation
  layer in sequence.
  """

  def __init__(self,
               in_channels,
               out_channels,
               kernel_size=3,   # 卷积核的尺寸, 默认为3
               stride=1,    # 卷积核移动的步长,默认为1
               padding=1,   # 填充宽度默认为1
               dilation=1,    # 在卷积核中填充dilation rate-1个0    空洞卷积（dilated convolution） dilation rate =1即退化为普通的卷积
               add_bias=False,
               upsample=False,
               fused_scale=False,
               wscale_gain=np.sqrt(2.0),
               activation_type='lrelu'):
    """Initializes the class with block settings.

    Args:
      in_channels: Number of channels of the input tensor fed into this block.
      out_channels: Number of channels of the output tensor.
      kernel_size: Size of the convolutional kernels.
      stride: Stride parameter for convolution operation.
      padding: Padding parameter for convolution operation.
      dilation: Dilation rate for convolution operation.
      add_bias: Whether to add bias onto the convolutional result.
      upsample: Whether to upsample the input tensor before convolution.
      fused_scale: Whether to fused `upsample` and `conv2d` together, resulting
        in `conv2d_transpose`.
      wscale_gain: The gain factor for `wscale` layer.
      activation_type: Type of activation function. Support `linear`, `lrelu`
        and `tanh`.

    Raises:
      NotImplementedError: If the input `activation_type` is not supported.
    """
    super().__init__()

    self.pixel_norm = PixelNormLayer()  

    if upsample and not fused_scale:
      self.upsample = ResolutionScalingLayer()  # 上采样提高分辨率
    else:
      self.upsample = nn.Identity()   # 不进行上采样. 仅占位, 不区分参数

    if upsample and fused_scale:    # fused_scale=True, 则融合upsample和conv2d
      
      # 当给一个特征图a, 以及给定的卷积核设置，我们分为三步进行逆卷积操作：
      # 第一步：对输入的特征图a进行一些变换，得到新的特征图a’，专业名词叫做interpolation，也就是插值。
      # 新的特征图：Height'=Height+(Stride-1)*(Height-1)，Width同样的   即将特征图插值（上采样）
      # 第二步：求新的卷积核设置，得到新的卷积核设置
      # 新的卷积核：Stride'=1这个数不变，无论你输入是什么。kernel的size′也不变,=Size, padding′为Size−padding−1.
      # 第三步：用新的卷积核在新的特征图上做常规的卷积，得到的结果就是逆卷积的结果，就是我们要求的结果。

      # 例：输入特征图A：3∗3
      # 输入卷积核K：kernel为 3∗3， stride为2，padding为1
      # 新的特征图A’：3 + (3-1)*(2-1) = 3+2 = 5，注意加上padding之后才是7。
      # 新的卷积核设置K’: kernel不变，stride为1，padding=3−1−1=1
      # 最终结果：( 5 + 2 − 3 ) / 1 + 1 = 5 (5+2-3)/1+1=5(5+2−3)/1+1=5

      self.use_conv2d_transpose = True  # 逆卷积ConvTranspose2d（学术名叫fractionally-strided convolutions）
      self.weight = nn.Parameter(   # 权重， 放入网络迭代优化
          torch.randn(kernel_size, kernel_size, in_channels, out_channels))
      fan_in = in_channels * kernel_size * kernel_size
      self.scale = wscale_gain / np.sqrt(fan_in)    # wscale_gain = 根号2
    else:
      self.use_conv2d_transpose = False
      # Conv2d：二维卷积运算，即卷积层
      self.conv = nn.Conv2d(in_channels=in_channels,
                            out_channels=out_channels,
                            kernel_size=kernel_size,
                            stride=stride, 
                            padding=padding,
                            dilation=dilation,
                            groups=1,
                            bias=add_bias)

    self.wscale = WScaleLayer(in_channels=in_channels,
                              out_channels=out_channels,
                              kernel_size=kernel_size,
                              gain=wscale_gain)

    # 选择激励函数
    if activation_type == 'linear':
      self.activate = nn.Identity()
    elif activation_type == 'lrelu':
      self.activate = nn.LeakyReLU(negative_slope=0.2, inplace=True)
    elif activation_type == 'tanh':
      self.activate = nn.Hardtanh()
    else:
      raise NotImplementedError(f'Not implemented activation function: '
                                f'{activation_type}!')

  def forward(self, x): # 依次调用各层
    x = self.pixel_norm(x)
    x = self.upsample(x)

    if self.use_conv2d_transpose:
      kernel = self.weight * self.scale
      kernel = F.pad(kernel, (0, 0, 0, 0, 1, 1, 1, 1), 'constant', 0.0)
      kernel = (kernel[1:, 1:] + kernel[:-1, 1:] +
                kernel[1:, :-1] + kernel[:-1, :-1])
      kernel = kernel.permute(2, 3, 0, 1)
      x = F.conv_transpose2d(x, kernel, stride=2, padding=1)  # 进行逆卷积运算
      x = x / self.scale
    else:
      x = self.conv(x)
      
    x = self.wscale(x)
    x = self.activate(x)
    return x
