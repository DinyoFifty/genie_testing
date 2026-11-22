# Genie Testing

Collect distance measurements from an initiator and reflector, save report figures, and calculate calibration recommendations.

## First-time setup

The tested setup uses 64-bit Windows and BRD2606A boards. Install these applications once on each laptop.

- [Visual Studio Code](https://code.visualstudio.com/).
- [Simplicity Studio 6 and Simplicity Installer](https://www.silabs.com/software-and-tools/simplicity-studio).

Extract the entire ZIP into a short local path such as `C:\Projects\genie_testing`. Double-click [setup.cmd](setup.cmd) in **File Explorer**. It opens the project in VS Code when finished, so you do not need to open VS Code first. Keep the setup window open until it says setup is complete. An internet connection is needed for missing downloads.

Setup reuses an available 64-bit Python **3.11**, or downloads the official **3.11.9** Windows installer and installs it for your Windows account. It verifies the download's SHA-256 checksum and continues without requiring a terminal restart. An existing Python of another version can remain installed.

Using the command line tool included with Simplicity Installer, setup installs missing Simplicity SDK **2026.6.2**, Channel Sounding Analyzer **1.2.2**, ARM GCC **14.2.rel1**, Simplicity Commander, CMake, and Ninja packages. Large SDK downloads can take several minutes. If Silicon Labs requires a sign-in or package access, setup prints the affected packages; open Simplicity Installer to complete that step, then run setup again.

Setup also creates or repairs the project's local Python environment, installs and checks the Python dependencies, installs the Python, Pylance, Python Debugger, C/C++, and Genie Files extensions, refreshes SDK and compiler paths, and opens [genie_testing.code-workspace](genie_testing.code-workspace). Run setup again after moving the project or pulling updates. It reuses the tools and extensions already installed. These installation scripts support Windows.

If setup fails, send the complete error and [data/setup/setup.log](data/setup/setup.log). Once Python starts, [data/setup/setup-report.json](data/setup/setup-report.json) also records the failed stage, discovered tool paths, and checked package versions. Both files stay local and are excluded from Git and the teammate ZIP.

If VS Code asks, trust your extracted project and select its `.venv\Scripts\python.exe` with **Python > Select Interpreter**. Use **Run Python File** for the scripts below. The commands below also work directly in the terminal. Restart VS Code if a newly installed extension requests it.

## Main workflow

### 1. Choose the antenna mode

Run [calibration.py](calibration.py) and choose physical ANT1 or ANT2 for isolated calibration.

```powershell
.\.venv\Scripts\python.exe calibration.py
```

Run [normal.py](normal.py) for testing with both antennas.

```powershell
.\.venv\Scripts\python.exe normal.py
```

Each script configures both firmware projects and preserves your saved physical antenna offsets.

Mode, antenna offsets, board assignments, build paths, and editor settings stay local. Setup creates missing configuration files from [shared defaults](support/local_defaults/) and preserves existing ones. A new checkout starts in normal mode with zero wireless offsets; calibrate the board pair you will use.

### 2. Edit offsets when applying a calibration result

The **Initiator offsets** and **Reflector offsets** groups in Genie Files open these headers.

- [Initiator offsets](firmware/bt_cs_ncp/config/sl_rail_util_cs_antenna_offset_config.h)
- [Reflector offsets](firmware/bt_cs_soc_reflector_studio/config/sl_rail_util_cs_antenna_offset_config.h)

Edit `GENIE_ANT1_WIRELESS_OFFSET_CM` or `GENIE_ANT2_WIRELESS_OFFSET_CM` for the antenna being calibrated. Values are in centimeters. Save both headers before flashing. Follow the recommendation for each board's captured baseline.

Simulated results exercise the software. Physical measurements determine the offsets to apply to your boards.

### 3. Build and flash

Run [flash.py](flash.py) with a board connected by USB.

```powershell
.\.venv\Scripts\python.exe flash.py
```

For a new board, enter `1` for initiator or `2` for reflector. The launcher remembers the J-Link serial number. Recognized board nicknames also identify the role.

The launcher builds the matching firmware project from saved source and programs the connected board. Connect the other board and repeat as needed. Flash both boards after changing mode or offsets.

### 4. Collect measurements

Connect the initiator by USB, power the reflector, and run [analyzer.py](analyzer.py).

```powershell
.\.venv\Scripts\python.exe analyzer.py
```

Genie opens the installed standalone Channel Sounding Analyzer. Simplicity Studio can stay closed during collection.

Choose `1` for a single trial or `2` for guided trials. Both choices use the saved antenna configuration.

- Enter actual separation and board heights in meters.
- Enter each board's yaw in degrees, such as `0`, `+45`, or `-45`.
- Mark a consistent zero direction. Clockwise rotation viewed from above is positive.
- Type `skip` for an unknown condition.
- Reuse the saved heights when they stay the same.

Close the Genie graph or press **Ctrl+C** to stop and export the collected readings. A loss of new readings also stops the trial and saves its partial results.

#### Guided trials

Guided collection works in isolated calibration mode and normal dual-antenna mode.

The checklist has 40 conditions, covering 1, 2, 5, 10, and 20 m at eight orientations in 45-degree steps. Select the rotating board and enter the fixed setup once. You can jump to another distance or orientation and resume a compatible batch later.

Each completed trial uses the first 100 finite filtered distance estimates. Its raw capture retains the complete received stream. Interrupted trials save their collected readings and remain pending.

Progress is stored under [data/calibration/](data/calibration/) as batches are created. Saved board identities, antenna mode, offsets, and setup distinguish compatible batches.

### 5. Analyze the trials

Run [linearreg.py](linearreg.py) and select the trial folders to analyze together.

```powershell
.\.venv\Scripts\python.exe linearreg.py
```

For a full calibration batch, select the 40 completed trials for one physical antenna and one offset baseline. The tool combines repeated trials within an orientation, then takes the median of the orientation results separately at each actual distance. It fits the five summarized distance points.

Repeated trials at one distance provide a constant-bias estimate with an assumed slope of 1. Multiple distances let the fit estimate slope and intercept.

Guided isolated-antenna captures supply the saved antenna and offset baseline for recommendations. Normal-mode captures support accuracy plots and regression.

Apply physical calibration recommendations to the headers, flash both boards, and collect fresh measurements to check the result. After calibrating ANT1 and ANT2, run [normal.py](normal.py), flash both boards, and proceed with dual-antenna testing.

## Find the results

Each trial appears in [data/live/](data/live/) under a name such as `2026-11-21_4-14PM`.

| Output | Purpose |
| --- | --- |
| `measurements.xlsx` | Measurements and trial conditions |
| `readings_in_order.png` | Reading sequence and actual-distance reference |
| `filtered_distance_distribution.png` | Distribution and actual-distance reference |
| `capture/raw.jsonl` | Original received records |
| `capture/measurements.csv` | Exported measurement rows |
| `capture/summary.json` | Trial conditions, statistics, and completion information |

The PNGs are saved at 300 dpi. A supplied actual distance appears as a red dotted line. Reading order uses sample number.

Deleting a trial folder removes its contained raw capture and reports. The Genie Files right-click **Delete** command sends live entries to the Windows Recycle Bin.
Hold **Ctrl** while clicking to select several live entries, or use **Shift** to select a range. Right-click a selected entry and choose **Delete** to move the selection together. Selecting a folder includes its contents.

Finalized source recordings are automatically recycled after their contents have been verified against the saved raw capture and reports have been exported. Temporary `.crswap` files can contain readings from an interrupted recording and require a duplicate check before cleanup.

## Share with teammates

Send `genie_testing.zip`. It contains the five main scripts, setup, the sidebar installer, helper code, and complete copies of both firmware projects. Each teammate extracts it and follows **First-time setup**.

The ZIP starts with an empty [data/live/](data/live/). Captures, trial progress, reports, logs, Python environments, build caches, board assignments, and local configuration files are excluded from Git and the ZIP. Teammates get the shared defaults and calibrate their own boards. Share selected trial folders separately when needed.

To make a fresh ZIP after changing the code, run this command from the project terminal.

```powershell
.\.venv\Scripts\python.exe -B support\package_project.py
```

The packager writes `genie_testing.zip` beside the project folder and includes actual firmware contents even when the local firmware folders are junctions. It also rebuilds the bundled Genie Files extension from [support/vscode-workflow/](support/vscode-workflow/).
