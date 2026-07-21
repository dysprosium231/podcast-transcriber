# whisper-project：播客自动转录 + 双语翻译工具（Windows）

监控你订阅的播客 RSS，发现新一期就自动下载、用本地 GPU 转录成英文文字稿、调用你配置的翻译服务生成中文翻译，最后产出一个可点击跳转播放的双语字幕网页。全程无人值守：Windows 计划任务触发，原生系统通知汇报进度，转录/翻译过程中还有一个悬浮小窗实时显示进度。

## 特性

- **全自动**：Windows 计划任务定时触发，不需要手动跑命令
- **不抢显卡**：检测到你在玩全屏游戏会自动推迟，运行前还有一条可交互的确认通知（取消/延后/立即开始）
- **本地转录**：用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 在你自己的 GPU 上跑，音频不用上传到任何地方
- **可插拔翻译服务商**：翻译走 OpenAI 兼容接口，DeepSeek / OpenAI / Moonshot / 智谱等大部分服务商都能直接用，改配置就行，不用改代码
- **原生桌面反馈**：Windows Toast 通知 + 自绘悬浮进度窗（转圈等待 → 下载/转录/翻译进度条 → 完成变绿），不是网页或命令行输出
- **双语字幕播放页**：生成的 `subtitles.html` 带音频播放器，点哪句字幕就跳到哪句，中英对照

## 环境要求

- Windows 10/11
- NVIDIA 显卡（faster-whisper 用 CUDA 跑转录；`compute_type="float16"` 需要显卡支持）
- Python 3.10+（建议用 conda 单独建一个环境）
- 一个 OpenAI 兼容的翻译 API Key（默认示例用 [DeepSeek](https://platform.deepseek.com/)，换别的服务商也行）

## 安装

1. 克隆本仓库，装依赖：

   ```bat
   pip install -r requirements.txt
   ```

2. 下载一个 faster-whisper 格式（CTranslate2 转换过）的 Whisper 模型，放到 `models/<模型名>/` 下面，比如：

   ```
   models/large-v3/
     model.bin
     config.json
     tokenizer.json
     vocabulary.json / vocabulary.txt
   ```

   模型可以从 Hugging Face 上的 `Systran/faster-whisper-<size>` 系列仓库下载（如 `Systran/faster-whisper-large-v3`），或者用 `huggingface-cli download` 拉取。显卡显存不够的话可以换小一点的模型（`medium`、`small`、`base.en` 等），文件夹名要和下一步 `config.json` 里的 `whisper_model_size` 对上。

3. 复制配置模板并按需修改：

   ```bat
   copy config.example.json config.json
   ```

   编辑 `config.json`（见下方「配置说明」）。

4. 设置翻译服务的 API Key 环境变量（默认是 `DEEPSEEK_API_KEY`，具体看你 `config.json` 里的 `translation.api_key_env`）：

   ```bat
   setx DEEPSEEK_API_KEY "你的key"
   ```

5. 先手动跑一次确认没问题：

   ```bat
   python daily_podcast.py
   ```

## 配置说明（`config.json`）

```jsonc
{
  "feeds": {
    "节目显示名": "RSS地址"
  },
  "whisper_model_size": "large-v3",  // 对应 models/ 下的文件夹名
  "translation": {
    "provider_name": "DeepSeek",           // 只用于日志/展示
    "base_url": "https://api.deepseek.com", // OpenAI兼容接口地址
    "api_key_env": "DEEPSEEK_API_KEY",      // 从哪个环境变量读API Key
    "model": "deepseek-v4-flash",
    "extra_system_prompt": ""               // 可选：针对某个播客的专有名词纠错说明等
  }
}
```

`feeds` 里想加几个节目就加几个，key 会同时用作文件夹名和通知里的显示名。`extra_system_prompt` 是留给你补充的翻译提示（比如某个播客主持人的名字容易被语音识别听错，可以在这里说明），不填就用通用翻译提示词。

## 设置每日自动运行（Windows 计划任务）

1. 打开「任务计划程序」，新建任务
2. 触发器设成你想要的时间（比如每天早上10点）
3. 操作设成启动程序，目标填 `run_hidden.vbs` 的完整路径（这个文件会静默运行、不弹黑框）
4. 保存后可以在任务计划程序里右键「运行」手动测试一次

`run_hidden.vbs` → `run_daily.bat`（设置好 Python 环境的 PATH）→ `prompt_before_run.py`（弹确认通知，10分钟不操作自动开始）→ `daily_podcast.py`（真正干活）。项目文件夹整体挪动位置不需要改这几个脚本，只有计划任务里配置的目标路径需要跟着手动改一次。

如果你不用 conda，把 `run_daily.bat` 开头那几行换成你自己的 Python 环境激活方式即可。

## 目录结构

```
episodes/节目名/期数标题/
  audio.mp3          原始音频
  data.json           带时间戳的逐句中英文
  transcript_en.txt   纯英文稿
  transcript_zh.txt   纯中文稿
  subtitles.html      双语字幕播放页（打开这个看/听）
```

`transcribe.py` 是早期写的一个单文件转录小例子，不属于自动化主流程，仅供参考。

## License

MIT
