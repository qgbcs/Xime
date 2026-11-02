package com.kingzcheung.xime.ui.keyboard

import androidx.compose.ui.unit.dp

/**
 * 圆形图标按钮的统一规格：30dp 圆底/触控区 + 22dp 图标。
 *
 * 候选栏（logo / 返回 / 收起 / 工具栏）与菜单栏（关闭 / 设置）共用同一套数字：
 * 同类按钮各写一套尺寸会出现"同一个位置、换个图标就变大变小"的观感断裂。
 * logo 是填满画布的实心矢量，与线性图标同 dp 时视觉量相当，不要为它单独缩尺寸。
 */
internal val IconButtonSize = 30.dp
internal val IconButtonGlyphSize = 22.dp

/**
 * 图标按钮所在栏（候选栏 / 菜单栏顶栏）的统一高度。
 *
 * 两者占据同一个位置且互相切换：栏高不同会让按钮中心对不齐，
 * 切换时出现几个 dp 的上下跳动——按钮尺寸统一了，容器也得跟着统一。
 */
internal val IconButtonRowHeight = 44.dp
