from tqdm import tqdm
import copy

import torch
import torch.nn as nn
import torch.optim as optim


# 选取梯度下降算法
def get_inversion(inversion_type, args):
    if inversion_type == 'GD':      # SGD: 随机梯度下降
        return GradientDescent(args.iterations, args.lr, optimizer=optim.SGD, args=args)
    elif inversion_type == 'Adam':  # Adam:自适应的动量梯度下降 效果较好
        return GradientDescent(args.iterations, args.lr, optimizer=optim.Adam, args=args)


class GradientDescent(object):
    def __init__(self, iterations, lr, optimizer, args):
        self.iterations = iterations
        self.lr = lr
        self.optimizer = optimizer
        self.init_type = args.init_type  # ['Zero', 'Normal']       # 随机初始化方式, zero()或者randn()

    # 逆映射,生成图像
    # latent_estimates, history = inversion.invert(generator, y_gt, loss, batch_size=1, video=args.video)
    def invert(self, generator, gt_image, loss_function, batch_size=1, video=False, *init):
        input_size_list = generator.input_size()    #  def input_size(self):
                                                        #return [(self.z_number, self.z_dim), (self.z_number, self.layer_c_number)]
        if len(init) == 0:
            if generator.init is False:     # 如果generator没有初始化，则再进行一遍generator本来进行的初始化
                latent_estimate = []
                for input_size in input_size_list:
                    if self.init_type == 'Zero':
                        latent_estimate.append(torch.zeros((batch_size,) + input_size).cuda())
                        # latent_estimate尺寸为：A(batch_size, self.z_number, slef.z_dim)和 B(batch_size, self.z_number, slef.layer_c_number)
                        # layer_c_number的值为分割的那一层的z_dim， 默认的z_dim为512
                        # A是用来生成预计结果的，B是生成alpha的， 即加权矩阵
                    elif self.init_type == 'Normal':
                        latent_estimate.append(torch.randn((batch_size,) + input_size).cuda())
            else:
                # generator.init_value()方法和上面初始化的方式一样，都是随机生成预计结果和alpha的
                latent_estimate = list(generator.init_value(batch_size))    # 随机初始化estimate： return [z_estimate, z_alpha]
        else:
            assert len(init) == len(input_size_list), 'Please check the number of init value'
            latent_estimate = init

        for latent in latent_estimate:
            latent.requires_grad = True
        # 将z_estimate和z_alpha放入优化器迭代优化
        optimizer = self.optimizer(latent_estimate, lr=self.lr)

        history = []
        # Opt
        # tqdm是一个便捷的进度条封装器, 可以封装任意的迭代器以在终端显示进度条
        for i in tqdm(range(self.iterations)):
            y_estimate = generator(latent_estimate)  # 使用latent code合成图像，generator是加载的预训练model
            optimizer.zero_grad()       # 优化器清除缓存
            # 用神经网络生成的图像与输入计算loss，反过来优化latent_estimate，
            # 最后返回的不是网络生成的y_estimate，而是latent_estimate
            loss = loss_function(y_estimate, gt_image)  # 计算loss
            loss.backward()         # 梯度值回溯
            optimizer.step()        # 优化
            if video:
                history.append(copy.deepcopy(latent_estimate))
        return latent_estimate, history

