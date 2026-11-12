# GwentTracker · 昆特牌记牌器

Windows 平台的《巫师之昆特牌》(GWENT) 第三方记牌器。通过只读方式读取游戏进程内存，
自动记录双方出牌、实时对比人口（战力）与局分，并基于热门卡组库预测对手卡组，
另提供可拖动悬浮窗。

> ⚠️ **免责声明**：本工具仅读取游戏内存，不写入、不修改游戏文件与代码，与 CD PROJEKT RED
> 官方无任何关联。使用任何第三方辅助工具都可能违反游戏用户协议，存在账号被警告、限制或
> 处罚的风险，请自行评估并自愿承担全部后果。请勿将本工具用于任何商业用途。

## 功能

- 自动对局识别：进入对局、小局切换、对局结束自动开始/结束记录，无需手动点击
- 出牌记录：按小局、阵营（我方/敌方）分类，记录卡牌、人口、次数，可删除选中或清空
- 人口对比：实时读取双方人口上限（150 + 领袖人口）与当前人口，含进度条与差值提示
- 卡组预测：内置 330 套热门卡组，按对手已出牌实时匹配，给出匹配度与可能卡牌
- 悬浮窗：置顶半透明窗，显示对手卡组预测与实时人口，支持隐藏/拖动/缩放
- 7 套主题：猎魔（HUD）/ 浅色 / 暗色 / 翡翠 / 深海 / 暮紫 / 绯红，标题栏与窗口背景跟随主题
- 历史记录与统计：天梯胜率、阵营分布、连胜连败、近期趋势
- 增量自动更新：启动时比对服务端 manifest，按 sha1 差异只下载变更文件

## 目录结构

```
app/
  main.py          # 主程序：Tkinter UI、主题、表格、悬浮窗、更新逻辑（约 7000 行）
  mem_source.py    # frida 内存读取：注入、指针链、事件回调、对局状态解析
  card_db.py       # 卡牌数据库：卡名、人口、阵营、别名映射
  deck_predict.py  # 卡组预测：匹配算法、候选卡组、可能卡牌
  capture.py       # 屏幕/窗口捕获辅助
  glass.py         # Windows 毛玻璃、圆角、标题栏着色（DWM API）
cards_zh.json      # 中文卡牌数据
offsets.json       # 内存偏移配置
icon.ico           # 应用图标
```

## 本地运行（开发）

依赖：Python 3.11、Windows 10/11。

```bash
pip install pillow pywin32 frida customtkinter
python app/main.py
```

`customtkinter` 为可选依赖：缺失时自动回退到原生 Tkinter（圆角胶囊退化为直角）。

## 打包

```bash
python -m PyInstaller --noconfirm --onedir --windowed --name GwentTracker \
  --icon icon.ico --hidden-import frida --collect-all frida --collect-all customtkinter \
  --exclude-module numpy --exclude-module psutil --exclude-module yaml --exclude-module cv2 \
  app/main.py
```

打包后需将 `icon.ico`、`offsets.json`、`cards_zh.json`、`_internal/decks_builtin.json`
放到 exe 同级目录。

## 更新与发布

客户端启动时请求 `http://47.116.199.195/gwent/app/manifest.json`，比对
`files[rel].sha1` 后增量下载 `/gwent/app/<rel>`。发布新版本需要：

1. 构建新的 `GwentTracker.exe` 与 `_internal`
2. 生成新的 `manifest.json`（version / notes / files）
3. 上传变更文件与新 manifest 到 `/gwent/app/`
4. 重新打包 `GwentTracker_latest.zip` 供新用户下载

## 下载

- 官网：https://ntemews.cn/gwent/landing/
- 夸克网盘：https://pan.quark.cn/s/6554ec224270

## 许可证

MIT License，见 [LICENSE](LICENSE)。
