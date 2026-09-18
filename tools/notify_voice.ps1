param(
    [string]$Message = "제미나이 작업이 종료되었습니다."
)

try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voices = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture -like "ko*" -and $_.Enabled }
    if ($voices) {
        $synth.SelectVoice($voices[0].VoiceInfo.Name)
    }
    $synth.Rate = 1
    $synth.Speak($Message)
} catch {
    # 오디오 장치가 없거나 백그라운드 환경일 경우 무시
}
