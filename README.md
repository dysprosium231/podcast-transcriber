# whisper-project：播客自动转录 + 双语翻译工具（Windows）

监控你订阅的播客 RSS，发现新一期就自动下载、用本地 GPU 转录成英文文字稿、调用你配置的翻译服务生成中文翻译，最后产出一个可点击跳转播放的双语字幕网页。全程无人值守：Windows 计划任务定时触发，原生系统通知汇报进度，转录/翻译过程中还有一个悬浮小窗实时显示状态。

## 快速开始

按顺序做完这几步，就能有一个每天自动跑的播客转录+翻译流水线。全程建议用图形界面（`setup_wizard.py` 或打包好的 `podcast-manager.exe`），不用手动编辑配置文件——如果你更习惯命令行，跳到最后的[《不用图形界面》](#不用图形界面纯命令行)。

### 第1步：确认环境

- Windows 10/11
- 一块 NVIDIA 显卡（本地转录要用 CUDA 跑 GPU 推理）
- 一个翻译服务的 API Key，比如 [DeepSeek](https://platform.deepseek.com/)（注册后在控制台生成一个 Key，等下第4步会用到；换成 OpenAI/Moonshot 等其他服务商也可以）

### 第2步：装好运行环境

不管你打算用命令行还是图形界面，转录/翻译本身都需要一个装好依赖的 Python 环境——**图形界面只是操作面板，真正干活的还是这一步装的环境**。

1. 装 [Miniconda](https://docs.conda.io/en/latest/miniconda.html)（推荐用 conda，理由见下面的提示框），建一个专用环境：

   ```bat
   conda create -n whisper-env python=3.11
   conda activate whisper-env
   ```

2. **装好 CUDA 运行库**——这一步很容易被忽略，但没它转录会直接报错崩溃。`pip install faster-whisper` 并不会带上它依赖的 cuBLAS / cuDNN 这些 CUDA 运行库，必须单独装：

   ```bat
   conda install -c conda-forge cudnn libcublas cuda-nvrtc
   ```

3. 克隆本仓库，装 Python 依赖：

   ```bat
   pip install -r requirements.txt
   ```

4. 复制配置模板：

   ```bat
   copy config.example.json config.json
   ```

5. Whisper 转录模型不需要手动下载，第一次用的时候会自动从 Hugging Face 下载好并缓存（下一步图形界面里也能手动点下载、看进度）。显卡显存不够的话可以换小一点的模型（`medium`、`small`、`base.en` 等），第4步里选。

> **为什么推荐 conda 而不是纯 pip venv**：CUDA 运行库在 Windows 上不好装——完整装 NVIDIA CUDA Toolkit 版本要跟 ctranslate2（faster-whisper 的推理引擎）编译时用的版本精确匹配，很容易踩坑；conda-forge 把这些库打成了普通 conda 包，装起来跟装其他 Python 包没区别，版本也管理得比较省心。如果你不想用 conda，也可以试试 pip 装 `nvidia-cublas-cu12`、`nvidia-cudnn-cu12` 这类官方 CUDA 轮子，但没有像 conda 这条路验证得那么充分。

> **遇到 `cublas64_12.dll` / `cudnn64_9.dll` 之类"找不到 xxx.dll"的报错**：说明 CUDA 运行库没装好或者版本不匹配，回到第2步重新确认。可以用下面这行快速自检（在装好依赖的环境里跑，不报错就说明能找到）：
>
> ```bat
> python -c "import ctypes; ctypes.WinDLL('cublas64_12.dll'); print('OK')"
> ```

### 第3步：打开图形界面

```bat
python setup_wizard.py
```

或者不想碰命令行：去 [Releases](../../releases) 页面下载打包好的 `podcast-manager.exe`，放在项目根目录（跟 `config.json` 同一层）双击打开。这个 exe 只是个轻量的配置/管理面板，本身不做转录翻译——那部分工作还是交给第2步装好的 Python 环境，通过它调用执行，所以第2步不能省。首次打开 exe 时 Windows 可能会先扫描一下，等个几秒到几十秒，之后就快了。

打开后是三个页签：**播客库**（浏览已经生成的内容，第一次打开是空的）、**手动处理**（后面讲）、**设置**——现在去 **设置** 页签。

### 第4步：在「设置」页签里填好这几样

1. **播客订阅**：节目名随便起（会用作文件夹名），RSS 地址填播客的订阅源
2. **翻译服务**：下拉选个预设（比如 DeepSeek），把第1步拿到的 API Key 粘贴进去
3. **Whisper 转录模型**：选个模型大小，点「下载此模型」（如果还没下载好的话）
4. **每日自动运行**：勾选「启用每日自动运行」，选好每天几点跑，点「应用定时任务设置」——这一步会自动帮你建好 Windows 计划任务，不用再去系统的任务计划程序里手动配置
5. 最下面点 **保存配置**

### 第5步：测试一次

回到「手动处理」页签的「单个文件」子页签，随便选一个本地音频文件，填个节目名和标题，点「开始转录+翻译」。这一步是为了在正式开始每天自动跑之前，确认转录/翻译整条链路能正常跑通（第一次跑会先花1-2分钟加载GPU模型，之后同一次运行里就不用再等了）。跑完之后回到「播客库」页签，应该能看到这一条，点「打开字幕页」看看效果。

### 完成

到这里就设置好了。以后每天到点，Windows 会自动触发：先弹一条可以选择"取消/延后/立即开始"的确认通知（不操作会自动开始），然后是一个悬浮小窗显示下载/转录/翻译的实时进度，跑完弹通知，点通知能直接打开生成的字幕页。想看某一期的结果，随时打开「播客库」页签找就行。

## 特性一览

- **全自动**：Windows 计划任务定时触发，不需要手动跑命令
- **不抢显卡**：检测到你在玩全屏游戏会自动推迟，运行前还有一条可交互的确认通知（取消/延后/立即开始）
- **本地转录**：用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 在你自己的 GPU 上跑，音频不用上传到任何地方
- **可插拔翻译服务商**：翻译走 OpenAI 兼容接口，DeepSeek / OpenAI / Moonshot / 智谱等大部分服务商都能直接用，改配置就行，不用改代码
- **原生桌面反馈**：Windows Toast 通知 + 自绘悬浮进度窗（转圈等待 → 下载/转录/翻译进度条 → 完成变绿），不是网页或命令行输出
- **双语字幕播放页**：生成的 `subtitles.html` 带音频播放器，点哪句字幕就跳到哪句，中英对照
- **图形化管理界面**：`setup_wizard.py` / `podcast-manager.exe`——
  - **播客库**：浏览已生成的节目，直接打开字幕页 / 音频 / 所在文件夹
  - **手动处理**：daily_podcast.py 正常运行只抓每个节目最新一期，这里补三种不依赖自动抓取的方式——单个本地文件转录翻译；把某个节目（或临时粘贴的RSS地址）的完整历史列出来，勾选任意几期批量下载处理；选一个文件夹或zip压缩包批量导入本地音频归档。批量场景支持排队处理、随时取消剩余、开始前一次性询问是否覆盖已处理内容
  - **设置**：订阅、翻译服务商、模型下载、计划任务开关，全部和 `config.json` 保持同步，不用手动改配置文件
  - 界面进程和实际执行转录/翻译的进程是分开的（管理界面很轻量，重活交给子进程做），所以不管是 `python setup_wizard.py` 还是打包的 exe，启动都很快，也不存在"exe里缺CUDA库"的问题

## 配置文件参考（`config.json`）

图形界面会帮你生成和维护这个文件，一般不需要手动改，这里只是给想直接编辑或者想了解每个字段含义的人看：

```jsonc
{
  "feeds": {
    "节目显示名": "RSS地址"
  },
  "whisper_model_size": "large-v3",  // 对应 models/ 下的文件夹名
  "python_exe": "",                  // 可选：手动处理用来跑转录/翻译的真实python.exe路径，
                                      // 留空会自动从 run_daily.bat 的 CONDA_ENV 探测
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

如果想手动指定 Whisper 模型文件（比如离线环境、或者已经下载好了别的来源的模型），把模型文件夹放到 `models/<模型名>/` 下面（需要包含 `model.bin`、`config.json`、`tokenizer.json`、`vocabulary.json` 等文件），程序会优先用本地文件夹，不会再走自动下载。

## 目录结构（生成的内容在哪）

这些文件夹都是程序自动创建的，不用你自己手动建——`episodes/` 本身、每个节目的子文件夹、每一期的子文件夹，跑的时候会自动按需建好：

```
episodes/节目名/期数标题/
  audio.mp3          原始音频
  data.json           带时间戳的逐句中英文
  transcript_en.txt   纯英文稿
  transcript_zh.txt   纯中文稿
  subtitles.html      双语字幕播放页（打开这个看/听）
```

## 不用图形界面，纯命令行

如果不想用图形界面，装好[第2步](#第2步装好运行环境)的环境之后可以全程手动：

1. 编辑 `config.json`（字段说明见上方[《配置文件参考》](#配置文件参考configjson)）
2. 设置翻译服务的 API Key 环境变量（默认是 `DEEPSEEK_API_KEY`，具体看你 `config.json` 里的 `translation.api_key_env`）：

   ```bat
   setx DEEPSEEK_API_KEY "你的key"
   ```

3. 手动跑一次确认没问题：

   ```bat
   python daily_podcast.py
   ```

4. 设置每日自动运行（Windows 计划任务）：

   1. 打开「任务计划程序」，新建任务
   2. 触发器设成你想要的时间（比如每天早上10点）
   3. 操作设成启动程序，目标填 `run_hidden.vbs` 的完整路径（这个文件会静默运行、不弹黑框）
   4. 保存后可以在任务计划程序里右键「运行」手动测试一次，或者命令行执行 `schtasks /run /tn "任务名"`

   `run_hidden.vbs` → `run_daily.bat`（设置好 Python 环境的 PATH）→ `prompt_before_run.py`（弹确认通知，10分钟不操作自动开始）→ `daily_podcast.py`（真正干活）。项目文件夹整体挪动位置不需要改这几个脚本，只有计划任务里配置的目标路径需要跟着手动改一次。如果你不用 conda，把 `run_daily.bat` 开头那几行换成你自己的 Python 环境激活方式即可。

### 自己重新打包 exe

`pip install pyinstaller` 之后跑：

```bat
pyinstaller --onefile --windowed --name podcast-manager setup_wizard.py ^
  --add-binary "<conda环境>\Library\bin\tcl86t.dll;." ^
  --add-binary "<conda环境>\Library\bin\tk86t.dll;." ^
  --add-binary "<conda环境>\Library\bin\liblzma.dll;." ^
  --add-binary "<conda环境>\Library\bin\libbz2.dll;." ^
  --add-binary "<conda环境>\Library\bin\ffi.dll;." ^
  --add-binary "<conda环境>\Library\bin\libexpat.dll;." ^
  --add-binary "<conda环境>\Library\bin\sqlite3.dll;."
```

这几个 `--add-binary` 是因为 conda 环境的 tcl/tk 等运行时 DLL 不在 PyInstaller 默认能找到的位置，不加的话打包出来的 exe 会因为缺 DLL 打不开界面。

## 其他

- `transcribe.py` 是早期写的一个单文件转录小例子，不属于自动化主流程，仅供参考
- 项目文件夹整体挪动位置（换盘符/换目录）不需要改任何代码，只有 Windows 计划任务里配置的目标路径需要跟着手动改一次

## License

MIT
