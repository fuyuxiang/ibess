# 双人说话样例来源

`two_speaker_fixture.wav` 仅供受控自动化测试，不作为现场效果承诺，也不会自动登记为正式业务录音。

来源：官方ModelScope `iic/speech_campplus_sv_zh-cn_16k-common` 模型包提供的 `speaker1_a_cn_16k.wav`、`speaker2_a_cn_16k.wav`、`speaker1_b_cn_16k.wav`。模型README声明Apache License 2.0。

https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common

依次拼接speaker1_a、1秒静音、speaker2_a、1秒静音、speaker1_b、1秒静音、speaker2_a、1秒静音；约23.246秒。测试自动估计说话人数，不传固定两人数量。此样例无重叠说话，会议远场混响、同时说话需另行评测。
