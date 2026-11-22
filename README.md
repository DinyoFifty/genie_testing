# Genie Testing

Collect distance measurements from an initiator and reflector, save report figures, and calculate calibration recommendations.

## First-time setup

The tested setup uses Windows, Python 3.11, and BRD2606A boards. Install these tools once on each laptop.

- [Python 3.11](https://www.python.org/downloads/) with **Add Python to PATH** enabled.
- [Visual Studio Code](https://code.visualstudio.com/).
- [Simplicity Studio 6 and Silicon Labs tools](https://www.silabs.com/software-and-tools/simplicity-studio), including Simplicity SDK **2026.6.2**, Channel Sounding Analyzer **1.2.2**, ARM GCC **14.2.rel1**, Simplicity Commander, CMake **3.25 or newer**, and Ninja.

Extract the entire ZIP into a short local path such as `C:\Projects\genie_testing`. Double-click [setup.cmd](setup.cmd). An internet connection is needed for Python dependencies and the VS Code Python extension.

Setup checks the installed tools, creates the local Python environment, installs the Python dependencies and Genie Files sidebar, refreshes SDK and compiler paths, and opens [genie_testing.code-workspace](genie_testing.code-workspace). Run setup again after moving the project or installing missing tools. A missing prerequisite is printed in the setup window.

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

The ZIP starts with an empty [data/live/](data/live/). Your existing captures, Python environment, build caches, and remembered board assignments stay local. Source configuration headers carry the saved mode and offsets. Teammates should calibrate the boards they will use.

To make a fresh ZIP after changing the code, run this command from the project terminal.

```powershell
.\.venv\Scripts\python.exe -B support\package_project.py
```

The packager writes `genie_testing.zip` beside the project folder and includes actual firmware contents even when the local firmware folders are junctions. It also rebuilds the bundled Genie Files extension from [support/vscode-workflow/](support/vscode-workflow/).

### Send fixes through GitHub

The team repository is [DinyoFifty/genie_testing](https://github.com/DinyoFifty/genie_testing).

A ZIP contains the version created when it was packaged. A shared GitHub repository lets teammates download later fixes with **Source Control > Pull** or `git pull` after they have cloned it. Push your tested fix first. Save or commit each person's local code and calibration edits before pulling. The [GitHub update instructions](https://docs.github.com/en/get-started/using-git/getting-changes-from-a-remote-repository) explain cloning, pulling, and merge conflicts.

Run [setup.cmd](setup.cmd) after pulling changes to refresh dependencies, paths, and the sidebar. Changes to firmware source, antenna mode, or offsets also require [flash.py](flash.py) for each affected board.

Upload the complete firmware contents when creating the repository. Git handling of the original laptop's junctions can omit those sources. An extracted teammate ZIP has ordinary firmware directories suitable for the initial repository import.

For ZIP updates, extract the new version into a separate folder, run setup there, and copy the old [data/](data/) folder and any board-specific calibration headers you need to retain. Keep the previous copy until the updated project runs successfully.

### Help a teammate troubleshoot

Ask them to send the following information with each problem.

- The complete terminal error and the script or step that failed.
- Their code version or the ZIP date and whether setup completed.
- Board model, selected antenna mode, offset values, initiator COM port, and how the reflector is powered.
- The Analyzer error or log when connection fails. For a saved trial, include its `capture/summary.json` and the relevant recording when needed.

For collection, connect the initiator by USB and power the reflector from its battery. If several J-Link serial ports are present, specify the initiator's port for that terminal before running [analyzer.py](analyzer.py).

```powershell
$env:GENIE_INITIATOR_PORT = "COM6"
```

Use the actual port from Windows Device Manager. Run `python -B support/setup_genie.py --check` to verify installed prerequisites without modifying the project.

## Support and verification

The recording helpers, automated checks, dependency list, and sidebar source are under [support/](support/). These files support the main scripts.

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s support/tests -v
.\.venv\Scripts\python.exe analyzer.py --help
```

Automated tests use synthetic recordings and mocked hardware. Physical board operation is checked through actual captures.

The additional offline import, campaign, power, and software-correction commands remain available through `analyzer.py --help`. Their normalized fields are described in [DATA_DICTIONARY.md](support/docs/DATA_DICTIONARY.md). The offline importer requires a reviewed mapping for the source schema.
