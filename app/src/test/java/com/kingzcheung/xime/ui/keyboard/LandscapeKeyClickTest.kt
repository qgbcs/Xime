package com.kingzcheung.xime.ui.keyboard

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * issue #1130：横屏分体/紧凑键上滑后不应再触发 tap。
 *
 * [shouldEmitLandscapeClick] 是横屏键唯一的点击判定出口，只要本次手势已判为滑动
 * （含 `action: none` 的滑动吸收）就必须否决点击。
 */
class LandscapeKeyClickTest {

    @Test
    fun plainTapClicks() {
        assertTrue(
            shouldEmitLandscapeClick(
                clickEmitted = false,
                hasTriggeredSwipeUp = false,
                hasTriggeredSwipeDown = false,
                hasTriggeredSwipeLeft = false,
                hasTriggeredSwipeRight = false,
            )
        )
    }

    @Test
    fun swipeUpVetoesClick() {
        assertFalse(
            shouldEmitLandscapeClick(
                clickEmitted = false,
                hasTriggeredSwipeUp = true,
                hasTriggeredSwipeDown = false,
                hasTriggeredSwipeLeft = false,
                hasTriggeredSwipeRight = false,
            )
        )
    }

    @Test
    fun anySwipeDirectionVetoesClick() {
        val directions = listOf(
            booleanArrayOf(true, false, false, false),
            booleanArrayOf(false, true, false, false),
            booleanArrayOf(false, false, true, false),
            booleanArrayOf(false, false, false, true),
        )
        directions.forEach { (up, down, left, right) ->
            assertFalse(
                "swipe up=$up down=$down left=$left right=$right should veto click",
                shouldEmitLandscapeClick(false, up, down, left, right)
            )
        }
    }

    @Test
    fun secondClickInSameGestureIsDropped() {
        assertFalse(
            shouldEmitLandscapeClick(
                clickEmitted = true,
                hasTriggeredSwipeUp = false,
                hasTriggeredSwipeDown = false,
                hasTriggeredSwipeLeft = false,
                hasTriggeredSwipeRight = false,
            )
        )
    }
}
