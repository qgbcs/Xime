package com.kingzcheung.xime.speech

interface AsrBackend {
    val name: String
    
    fun setCallbacks(
        onResult: (String) -> Unit,
        onPartialResult: ((String) -> Unit)? = null,
        onStateChange: (RecognitionState) -> Unit,
        onError: (String) -> Unit
    )
    
    fun initialize(): Boolean
    fun start(): Boolean
    fun processAudioChunk(buffer: ByteArray)
    fun stop()
    fun cancel()
    fun release()
    /** 当前识别状态（由后端自行维护，默认空闲）。 */
    fun getState(): RecognitionState = RecognitionState.IDLE
    fun isAvailable(): Boolean
}
