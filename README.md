just adding randomized inputs and stop checks
don't expect any support I don't know what I'm doing, this is just an experiment while i update umplay

### training, races, events, skill purchasing, and starting runs

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Emulator Setup](#emulator-setup)
- [Changelog](#changelog)
- [Credits](#credits)

---

## Requirements

- Python 3.10
- Visual C++ Redistributable ([Download](https://aka.ms/vs/17/release/vc_redist.x64.exe))
- Android emulator (MuMu Player recommended) bluestacks sucks dont use it it will break screenshots for reasons i dont understand

---

## Installation

### Step 1: Clone the Repository


### Step 2: Install Anaconda

1. Download Anaconda: https://www.anaconda.com/download
2. Choose the 64-bit Windows Installer
3. During installation:
   - Check **"Add Anaconda to my PATH environment variable"**
   - Select **"Register Anaconda as my default Python"**
4. Complete the installation.

Also install Visual C++ Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe

### Step 3: Set Up Python Environment

```bash
conda create -n umamusume python==3.10
conda activate umamusume
python -m pip install -r requirements.txt
```

Type `y` and press Enter if prompted to proceed. This may take several minutes to complete.

### Step 4: Run the Bot

```bash
python main.py
```

Alternatively, run `start.bat` to launch the bot.

---

## Emulator Setup

### Display Settings
- **Resolution**: 720 x 1280 (Portrait mode)
- **DPI**: 180
- **FPS**: 30 or higher

### Graphics Settings
- **Rendering**: Standard
- **ADB**: Must be enabled in emulator settings

### Supported Emulators
- MuMu Player
---

## Configuration

1. Set graphics to `Standard` in-game (not `Basic`).    
2. Manually select your Uma Musume, Legacy Uma, and Support Cards before starting.    
3. Edit your runtime in main.py (default is 20 hours a day).

---

## Troubleshooting
good luck

### Bot Stuck in Menu
Disable "Keep alive in background" in emulator settings.

### ADB Connection Fails
Restart your machine

### Stats Not Showing in Scoring
Install or reinstall Visual C++ Redistributable:
- [Download vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe)

![Stats Display](https://github.com/user-attachments/assets/1f68af35-cf9d-41ce-9392-c26ecf83cc70)

---

## Credits

- **Original Repository**: [UmamusumeAutoTrainer](https://github.com/shiokaze/UmamusumeAutoTrainer) by [Shiokaze](https://github.com/shiokaze)
- **Global Server Port**: [UmamusumeAutoTrainer-Global](https://github.com/BrayAlter/UAT-Global-Server) by [BrayAlter](https://github.com/BrayAlter)
- **Sweepy Tosher ver** [Sweepy UAT](https://github.com/SweepTosher/umamusume-sweepy/tree/main)
- **Kimo's ver** [Sweepy UAT ala Kimo](https://github.com/KimochiDesu/umamusume-sweepy-kimo/)

---
