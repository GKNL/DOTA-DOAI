# -*- coding: utf-8 -*-

from __future__ import absolute_import, print_function, division


import tensorflow as tf
import tensorflow.contrib.slim as slim
from libs.configs import cfgs
from tensorflow.contrib.slim.nets import resnet_v1
from tensorflow.contrib.slim.nets import resnet_utils
from tensorflow.contrib.slim.python.slim.nets.resnet_v1 import resnet_v1_block
import tfplot as tfp


def resnet_arg_scope(
        is_training=True, weight_decay=cfgs.WEIGHT_DECAY, batch_norm_decay=0.997,
        batch_norm_epsilon=1e-5, batch_norm_scale=True):
    '''

    In Default, we do not use BN to train resnet, since batch_size is too small.
    So is_training is False and trainable is False in the batch_norm params.

    '''
    batch_norm_params = {
        'is_training': False, 'decay': batch_norm_decay,
        'epsilon': batch_norm_epsilon, 'scale': batch_norm_scale,
        'trainable': False,
        'updates_collections': tf.GraphKeys.UPDATE_OPS
    }

    with slim.arg_scope(
            [slim.conv2d],
            weights_regularizer=slim.l2_regularizer(weight_decay),
            weights_initializer=slim.variance_scaling_initializer(),
            trainable=is_training,
            activation_fn=tf.nn.relu,
            normalizer_fn=slim.batch_norm,
            normalizer_params=batch_norm_params):
        with slim.arg_scope([slim.batch_norm], **batch_norm_params) as arg_sc:
            return arg_sc


def fusion_two_layer(C_i, P_j, scope):
    '''
    i = j+1
    :param C_i: shape is [1, h, w, c]
    :param P_j: shape is [1, h/2, w/2, 256]
    :return:
    P_i
    '''
    with tf.variable_scope(scope):
        level_name = scope.split('_')[1]
        h, w = tf.shape(C_i)[1], tf.shape(C_i)[2]
        upsample_p = tf.image.resize_bilinear(P_j,
                                              size=[h, w],
                                              name='up_sample_'+level_name)
        # upsample_p = tf.image.resize_nearest_neighbor(P_j,
        #                                               size=[h, w],
        #                                               name='up_sample_' + level_name)
        reduce_dim_c = slim.conv2d(C_i,
                                   num_outputs=256,
                                   kernel_size=[1, 1], stride=1,
                                   scope='reduce_dim_'+level_name)

        add_f = 0.5*upsample_p + 0.5*reduce_dim_c
        return add_f
        # return fuse_f


def add_heatmap(feature_maps, name):
    '''

    :param feature_maps:[B, H, W, C]  包含所有通道的完整的feature map
    :return:
    '''

    def figure_attention(activation):
        fig, ax = tfp.subplots()
        im = ax.imshow(activation, cmap='jet')
        fig.colorbar(im)
        return fig

    heatmap = tf.reduce_sum(feature_maps, axis=-1)  # 在channel维上进行求和
    heatmap = tf.squeeze(heatmap, axis=0)  # 指定要删掉的为1的维度（batch维）
    tfp.summary.plot(name, figure_attention, [heatmap])


def enrich_semantics_supervised(net, channels, num_layer, scope):
    """
    TODO: Understand what is dilated convolutions(空洞卷积)?
    Step 1: The input feature map expands the receptive field by N dilated convolutions and a 1 × 1 convolutional layer.
    (the values of N take the numbers of{1, 1, 1, 1, 1} on pyramid levels P3 to P7)
    :param net:
    :param channels:  空洞卷积输出的通道数
    :param num_layer:  需要进行几次空洞卷积
    :param scope:
    :return:
    """
    with tf.variable_scope(scope):
        # 进行空洞卷积（具体作用如下：）
        # 1.扩大感受野
        # 2.捕获多尺度上下文信息（空洞卷积有一个参数可以设置dilation rate，具体含义就是在卷积核中填充 dilation rate-1 个0）
        for _ in range(num_layer-1):
            net = slim.conv2d(net, num_outputs=channels, kernel_size=[3, 3], stride=1, rate=2, padding="SAME")

        net = slim.conv2d(net, num_outputs=channels, kernel_size=[3, 3], stride=1, rate=4, padding="SAME")
        net = slim.conv2d(net, num_outputs=channels, kernel_size=[1, 1], stride=1, padding="SAME")
        return net


def generate_mask(net, num_layer, level_name):
    """
    InLD模块实现（上分支路部分）
    :param net: 输入的feature_dict中的某一层的feature map
    :param num_layer:
    :param level_name: 待处理的特征金字塔的某一层的名字Pi
    :return:
    """
    # Step 1: 对input feature进行空洞卷积和1*1卷积
    G = enrich_semantics_supervised(net=net,
                                    num_layer=num_layer,
                                    channels=256, scope="enrich_%s" % level_name)

    # channel维的维数
    last_dim = 2 if cfgs.BINARY_MASK else cfgs.CLASS_NUM + 1
    """两个分支之一：mask分支（通道数为cfgs.CLASS_NUM + 1）"""
    mask = slim.conv2d(G, num_outputs=last_dim, kernel_size=[1, 1], stride=1, padding="SAME",
                       activation_fn=None,
                       scope='gmask_%s' % level_name)

    act_fn = tf.nn.sigmoid if cfgs.SIGMOID_ON_DOT else None
    """两个分支之一：dot_layer分支"""
    dot_layer = slim.conv2d(G, num_outputs=256, kernel_size=[1, 1], stride=1, padding="SAME",
                            activation_fn=act_fn,
                            scope='gdot_%s' % level_name)

    return G, mask, dot_layer


def enlarge_RF(net, num_layer, k_size, rate):
    """
    扩大感受野（使用连续的空洞卷积实现）
    :param net:
    :param num_layer:
    :param k_size:
    :param rate:
    :return:
    """

    for _ in range(num_layer):
        net = slim.conv2d(net,
                          num_outputs=256, kernel_size=[k_size, k_size], stride=1, rate=rate)
    return net


def resnet_base(img_batch, scope_name, is_training=True):
    '''
    this code is derived from light-head rcnn.
    https://github.com/zengarden/light_head_rcnn

    It is convenient to freeze blocks. So we adapt this mode.
    '''
    if scope_name == 'resnet_v1_50':
        middle_num_units = 6
    elif scope_name == 'resnet_v1_101':
        middle_num_units = 23
    else:
        raise NotImplementedError('We only support resnet_v1_50 or resnet_v1_101. Check your network name....yjr')

    blocks = [resnet_v1_block('block1', base_depth=64, num_units=3, stride=2),
              resnet_v1_block('block2', base_depth=128, num_units=4, stride=2),
              resnet_v1_block('block3', base_depth=256, num_units=middle_num_units, stride=2),
              resnet_v1_block('block4', base_depth=512, num_units=3, stride=1)]
    # when use fpn . stride list is [1, 2, 2]

    with slim.arg_scope(resnet_arg_scope(is_training=False)):
        with tf.variable_scope(scope_name, scope_name):
            # Do the first few layers manually, because 'SAME' padding can behave inconsistently
            # for images of different sizes: sometimes 0, sometimes 1
            net = resnet_utils.conv2d_same(
                img_batch, 64, 7, stride=2, scope='conv1')
            net = tf.pad(net, [[0, 0], [1, 1], [1, 1], [0, 0]])
            net = slim.max_pool2d(
                net, [3, 3], stride=2, padding='VALID', scope='pool1')

    not_freezed = [False] * cfgs.FIXED_BLOCKS + (4-cfgs.FIXED_BLOCKS)*[True]
    # Fixed_Blocks can be 1~3

    with slim.arg_scope(resnet_arg_scope(is_training=(is_training and not_freezed[0]))):
        C2, end_points_C2 = resnet_v1.resnet_v1(net,
                                                blocks[0:1],
                                                global_pool=False,
                                                include_root_block=False,
                                                scope=scope_name)

    # C2 = tf.Print(C2, [tf.shape(C2)], summarize=10, message='C2_shape')
    # add_heatmap(C2, name='Layer2/C2_heat')

    with slim.arg_scope(resnet_arg_scope(is_training=(is_training and not_freezed[1]))):
        C3, end_points_C3 = resnet_v1.resnet_v1(C2,
                                                blocks[1:2],
                                                global_pool=False,
                                                include_root_block=False,
                                                scope=scope_name)

    # C3 = tf.Print(C3, [tf.shape(C3)], summarize=10, message='C3_shape')
    # add_heatmap(C3, name='Layer3/C3_heat')
    with slim.arg_scope(resnet_arg_scope(is_training=(is_training and not_freezed[2]))):
        C4, end_points_C4 = resnet_v1.resnet_v1(C3,
                                                blocks[2:3],
                                                global_pool=False,
                                                include_root_block=False,
                                                scope=scope_name)

    # add_heatmap(C4, name='Layer4/C4_heat')

    # C4 = tf.Print(C4, [tf.shape(C4)], summarize=10, message='C4_shape')
    with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
        C5, end_points_C5 = resnet_v1.resnet_v1(C4,
                                                blocks[3:4],
                                                global_pool=False,
                                                include_root_block=False,
                                                scope=scope_name)
    # C5 = tf.Print(C5, [tf.shape(C5)], summarize=10, message='C5_shape')
    # add_heatmap(C5, name='Layer5/C5_heat')

    feature_dict = {'C2': end_points_C2['{}/block1/unit_2/bottleneck_v1'.format(scope_name)],
                    'C3': end_points_C3['{}/block2/unit_3/bottleneck_v1'.format(scope_name)],
                    'C4': end_points_C4['{}/block3/unit_{}/bottleneck_v1'.format(scope_name, middle_num_units - 1)],
                    'C5': end_points_C5['{}/block4/unit_3/bottleneck_v1'.format(scope_name)],
                    # 'C5': end_points_C5['{}/block4'.format(scope_name)],
                    }
    for level in range(5, 1, -1):
        add_heatmap(feature_dict['C%d' % level], name='Layer%d/C%d_heat' % (level, level))

    pyramid_dict = {}
    with tf.variable_scope('build_pyramid'):
        with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(cfgs.WEIGHT_DECAY),
                            activation_fn=None, normalizer_fn=None):

            P5 = slim.conv2d(C5,
                             num_outputs=256,
                             kernel_size=[1, 1],
                             stride=1, scope='build_P5')
            pyramid_dict['P5'] = P5
            if (not cfgs.USE_SUPERVISED_MASK) and "P6" in cfgs.LEVLES:
                # if use supervised_mask, we get p6 after enlarge RF
                pyramid_dict['P6'] = slim.avg_pool2d(pyramid_dict["P5"], kernel_size=[2, 2],
                                                     stride=2, scope='build_P6')
            for level in range(4, 1, -1):  # build [P4, P3, P2]

                pyramid_dict['P%d' % level] = fusion_two_layer(C_i=feature_dict["C%d" % level],
                                                               P_j=pyramid_dict["P%d" % (level+1)],
                                                               scope='build_P%d' % level)
            for level in range(4, 1, -1):
                pyramid_dict['P%d' % level] = slim.conv2d(pyramid_dict['P%d' % level],
                                                          num_outputs=256, kernel_size=[3, 3], padding="SAME",
                                                          stride=1, scope="fuse_P%d" % level)
    for level in range(5, 1, -1):
        add_heatmap(pyramid_dict['P%d' % level], name='Layer%d/P%d_fpn_heat' % (level, level))

    if not cfgs.USE_SUPERVISED_MASK:
        print("we are in Pyramid::-======>>>>")
        print(cfgs.LEVLES)
        print("base_anchor_size are: ", cfgs.BASE_ANCHOR_SIZE_LIST)
        print(20 * "__")
        return [pyramid_dict[level_name] for level_name in cfgs.LEVLES]

    #
    # -----------------------------------------------------------------------------------------------------------------
    #
    mask_list = []  # 用于计算supervised_mask_loss
    with tf.variable_scope("enrich_semantics"):
        with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(cfgs.WEIGHT_DECAY),
                             normalizer_fn=None):
            for i, l_name in enumerate(cfgs.GENERATE_MASK_LIST):
                # 输入对应层的Feature Map，得到对应的mask和dot_layer
                G, mask, dot_layer = generate_mask(net=pyramid_dict[l_name],
                                                   num_layer=cfgs.ADDITION_LAYERS[i],
                                                   level_name=l_name)
                add_heatmap(G, name="MASK/G_%s" % l_name)
                add_heatmap(mask, name="MASK/mask_%s" % l_name)

                if cfgs.MASK_ACT_FET:  # InLD第二分支（dot_layer分支）与original feature map进行乘积运算
                    pyramid_dict[l_name] = pyramid_dict[l_name] * dot_layer
                mask_list.append(mask)

    with tf.variable_scope("enlarge_RF"):  # TODO：什么作用？？？ enlarge receptive field ?  增大感受野？
        with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(cfgs.WEIGHT_DECAY),
                             normalizer_fn=None):
            for i, l_name in enumerate(cfgs.ENLAEGE_RF_LIST):  # 再对每层进行空洞卷积以增大感受野
                pyramid_dict[l_name] = enlarge_RF(net=pyramid_dict[l_name],
                                                  num_layer=2, k_size=3, rate=2)
            if "P6" in cfgs.LEVLES:
                pyramid_dict['P6'] = slim.avg_pool2d(pyramid_dict["P5"], kernel_size=[2, 2],
                                                     stride=2, scope='build_P6')
                pyramid_dict["P6"] = slim.conv2d(pyramid_dict["P6"],
                                                 num_outputs=256, kernel_size=[3, 3], stride=1, rate=2)
    for level in range(5, 1, -1):
        add_heatmap(pyramid_dict['P%d' % level], name='Layer%d/P%d_lastheat' % (level, level))

    # return [P2, P3, P4, P5, P6]
    print("we are in Pyramid::-======>>>>")
    print(cfgs.LEVLES)
    print("base_anchor_size are: ", cfgs.BASE_ANCHOR_SIZE_LIST)
    print(20 * "__")
    return [pyramid_dict[level_name] for level_name in cfgs.LEVLES], mask_list


def restnet_head(inputs, is_training, scope_name):
    '''

    :param inputs: [minibatch_size, 7, 7, 256]
    :param is_training:
    :param scope_name:
    :return:
    '''

    with tf.variable_scope('build_fc_layers'):
        inputs = slim.flatten(inputs=inputs, scope='flatten_inputs')
        fc1 = slim.fully_connected(inputs, num_outputs=1024, scope='fc1')

        fc2 = slim.fully_connected(fc1, num_outputs=1024, scope='fc2')

        return fc2


