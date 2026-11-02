package com.kingzcheung.xime.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.longClick
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.graphics.Color
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.kingzcheung.xime.ui.keyboard.CandidateBar
import com.kingzcheung.xime.ui.keyboard.CandidateBarCallbacks
import com.kingzcheung.xime.ui.keyboard.CandidateBarState
import com.kingzcheung.xime.ui.keyboard.CandidateBarVisuals
import com.kingzcheung.xime.ui.keyboard.CandidatePage
import com.kingzcheung.xime.ui.keyboard.CandidatePageCallbacks
import com.kingzcheung.xime.ui.keyboard.CandidatePageState
import com.kingzcheung.xime.ui.keyboard.CandidateItem
import com.kingzcheung.xime.ui.theme.DividerColor
import com.kingzcheung.xime.ui.theme.KeyTextColor
import com.kingzcheung.xime.ui.theme.KeyboardBackground
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.Assert.assertEquals

@RunWith(AndroidJUnit4::class)
class CandidateBarTest {
    
    @get:Rule
    val composeTestRule = createComposeRule()
    
    @Test
    fun candidateBarDisplaysCandidates() {
        composeTestRule.setContent {
            CandidateBar(
                state = CandidateBarState.ChineseCandidates(
                    candidates = listOf("你好", "世界", "测试"),
                    inputText = "nihao",
                ),
                visuals = CandidateBarVisuals(
                    backgroundColor = KeyboardBackground,
                    textColor = KeyTextColor,
                    dividerColor = DividerColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { _, _ -> }
                )
            )
        }
        
        composeTestRule.onNodeWithText("你好").assertIsDisplayed()
        composeTestRule.onNodeWithText("世界").assertIsDisplayed()
        composeTestRule.onNodeWithText("测试").assertIsDisplayed()
    }
    
    @Test
    fun candidateBarHandlesEmptyCandidates() {
        composeTestRule.setContent {
            CandidateBar(
                state = CandidateBarState.Idle,
                visuals = CandidateBarVisuals(
                    backgroundColor = KeyboardBackground,
                    textColor = KeyTextColor,
                    dividerColor = DividerColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { _, _ -> }
                )
            )
        }
    }
    
    @Test
    fun candidateBarDisplaysInputTextWhenComposing() {
        composeTestRule.setContent {
            CandidateBar(
                state = CandidateBarState.ChineseCandidates(
                    candidates = listOf("你好"),
                    inputText = "nihao",
                ),
                visuals = CandidateBarVisuals(
                    backgroundColor = KeyboardBackground,
                    textColor = KeyTextColor,
                    dividerColor = DividerColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { _, _ -> }
                )
            )
        }
        
        composeTestRule.onNodeWithText("nihao").assertIsDisplayed()
    }
    
    @Test
    fun candidateBarDisplaysComments() {
        composeTestRule.setContent {
            CandidateBar(
                state = CandidateBarState.ChineseCandidates(
                    candidates = listOf("你好"),
                    comments = listOf("wubi"),
                    inputText = "nihao",
                ),
                visuals = CandidateBarVisuals(
                    backgroundColor = KeyboardBackground,
                    textColor = KeyTextColor,
                    dividerColor = DividerColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { _, _ -> }
                )
            )
        }
        
        composeTestRule.onNodeWithText("你好").assertIsDisplayed()
    }
    
    @Test
    fun candidateBarDisplaysAssociationCandidates() {
        composeTestRule.setContent {
            CandidateBar(
                state = CandidateBarState.ChineseCandidates(
                    candidates = listOf("你好"),
                    associationCandidates = listOf("世界", "吗"),
                    inputText = "nihao",
                ),
                visuals = CandidateBarVisuals(
                    backgroundColor = KeyboardBackground,
                    textColor = KeyTextColor,
                    dividerColor = DividerColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { _, _ -> }
                )
            )
        }
        
        composeTestRule.onNodeWithText("你好").assertIsDisplayed()
        composeTestRule.onNodeWithText("世界").assertIsDisplayed()
    }
    
    @Test
    fun candidateItemDisplaysText() {
        composeTestRule.setContent {
            CandidateItem(
                text = "测试候选词",
                index = 0,
                onClick = {},
                textColor = KeyTextColor
            )
        }
        
        composeTestRule.onNodeWithText("测试候选词").assertIsDisplayed()
    }
    
    @Test
    fun candidateItemDisplaysComment() {
        composeTestRule.setContent {
            CandidateItem(
                text = "你好",
                index = 0,
                onClick = {},
                textColor = KeyTextColor,
                comment = "aaaa"
            )
        }
        
        composeTestRule.onNodeWithText("你好").assertIsDisplayed()
        composeTestRule.onNodeWithText("aaaa").assertIsDisplayed()
    }

    @Test
    fun candidateBarLongPressOffersSecondScreenDemotion() {
        var action: String? = null
        val uiEvents = mutableListOf<Pair<String, String>>()
        composeTestRule.setContent {
            MaterialTheme {
                CandidateBar(
                    state = CandidateBarState.ChineseCandidates(
                        candidates = listOf("learned"),
                        inputText = "lear",
                    ),
                    visuals = CandidateBarVisuals(
                        backgroundColor = KeyboardBackground,
                        textColor = KeyTextColor,
                        dividerColor = DividerColor,
                    ),
                    callbacks = CandidateBarCallbacks(
                        onCandidateSelect = { _, _ -> },
                        onCustomPhraseAction = { _, selectedAction -> action = selectedAction },
                        onUiDiagnosticEvent = { event, detail -> uiEvents += event to detail },
                    ),
                    customPhraseWords = setOf("learned"),
                )
            }
        }

        composeTestRule.onNodeWithText("learned").performTouchInput { longClick() }
        composeTestRule.onNodeWithText("移到第二屏").assertIsDisplayed().performClick()
        assertEquals("moveSecondPage", action)
        composeTestRule.waitForIdle()
        assertEquals(
            listOf(
                "candidate_long_press" to "source=candidate_bar;candidate=learned",
                "candidate_menu_shown" to
                    "source=candidate_bar;candidate=learned;popup_focusable=false",
                "candidate_menu_selected" to
                    "source=candidate_bar;candidate=learned;action=moveSecondPage",
            ),
            uiEvents,
        )
        composeTestRule.onNodeWithText("learned").assertIsDisplayed()
    }

    @Test
    fun expandedCandidatePageLongPressCanDeleteCandidate() {
        var action: String? = null
        val uiEvents = mutableListOf<Pair<String, String>>()
        composeTestRule.setContent {
            MaterialTheme {
                CandidatePage(
                    state = CandidatePageState(
                        candidates = listOf("learned", "other"),
                        backgroundColor = Color.White,
                        textColor = Color.Black,
                    ),
                    callbacks = CandidatePageCallbacks(
                        onCandidateSelect = { _, _ -> },
                        onCustomPhraseAction = { _, selectedAction -> action = selectedAction },
                        onUiDiagnosticEvent = { event, detail -> uiEvents += event to detail },
                    ),
                    customPhraseWords = setOf("learned"),
                )
            }
        }

        composeTestRule.onNodeWithText("learned").performTouchInput { longClick() }
        composeTestRule.onNodeWithText("删除该词").assertIsDisplayed().performClick()
        assertEquals("delete", action)
        composeTestRule.onNodeWithText("other").assertIsDisplayed()
        assertEquals(
            listOf(
                "candidate_long_press" to "source=candidate_page;candidate=learned",
                "candidate_menu_shown" to
                    "source=candidate_page;candidate=learned;popup_focusable=false",
                "candidate_menu_selected" to
                    "source=candidate_page;candidate=learned;action=delete",
            ),
            uiEvents,
        )
    }
}