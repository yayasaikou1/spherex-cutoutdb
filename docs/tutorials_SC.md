# 使用说明
Release : `1.0.0rc3`.
## 安装
### 开发版安装
```bash
git clone xxxxx
```
在包目录下执行pip安装命令 (建议使用conda创建python3.11/3.12虚拟环境)
```bash
cd spherex_cutoutdb
python -m pip install -e ".[dev]"
```
安装完成后，请勿删除、移动或更名安装包目录，否则需要重新安装。
测试是否安装完成
```bash
spxcutdb --help
```

## 快速使用
### 准备输入星表

新建项目文件夹，例如spx_down
```bash
mkdir spx_down
cd spx_down
```
将输入星表保存到项目文件夹的根目录。输入星表，例如`input_catalog.csv`，应为csv格式，包含源名和坐标列，默认为`Name`, `RA_deg`, `DEC_deg`列。`Name`列必须是唯一ID，不允许重复，建议不要包含空格和不能用作文件名的非法字符。`RA_deg`和`DEG_deg`的单位必须为deg。如果存在`cutout_size_arcsec`列，可以为每个源指定不同的cutout_size（实验性功能，效果未验证）。

### 初始化项目和配置文件
在项目文件夹执行命令以初始化项目
```bash
spxcutdb init ./ --catalog input_catalog.csv --target-id-column Name #可以指定唯一id的列名
```
初始化命令会创建项目文件夹结构和配置文件`spherex_cutoutdb.yaml`, 可以自行修改该配置文件。
```bash
spxcutdb validate --project ./ --catalog input_catalog.csv
spxcutdb config diff --project ./  #显示当前config和默认配置的区别
```

### 下载校准文件
初始化项目后，需要下载校准文件（也可以自行填充到对应位置）
```bash
spxcutdb calibration sync --project ./ --product required --download-source cloud --max-workers 8
```
检验当前项目的校准产品
```bash
spxcutdb calibration validate --project ./
spxcutdb calibration status --project ./
```

### 获取目标源的观测信息
每次更新数据前，需要从IRSA网站获取当前源表的观测信息，预期不会花费太多时间。后续命令你可以尝试加`--verbose`输出过程信息
```bash
spxcutdb discover --project ./ --resume
```

### 使用流程1, 批量下载和批量测光
我们提供分离式的使用流程，对存储空间充足的用户（对180arcsec的cutout，大约5.1MB每张图），建议先执行批量下载命令，再执行测光。（建议下载到固态硬盘）

