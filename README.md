# Genie Testing

Collect distance measurements from an initiator and reflector, save report figures, and calculate calibration recommendations.

## First-time setup

The tested setup uses 64-bit Windows and BRD2606A boards. Install these applications once on each laptop.

- [Visual Studio Code](https://code.visualstudio.com/).
- [Simplicity Studio 6 and Simplicity Installer](https://www.silabs.com/software-and-tools/simplicity-studio).

On GitHub, choose **Code > Download ZIP**. Extract the entire ZIP into a short local path such as `C:\Projects\genie_testing`. Double-click [setup.cmd](setup.cmd) in **File Explorer**. It opens the project in VS Code when finished, so you do not need to open VS Code first. Keep the setup window open until it says setup is complete. An internet connection is needed for missing downloads.

Setup reuses an available 64-bit Python **3.11**, or downloads the official **3.11.9** Windows installer and installs it for your Windows account. It verifies the download's SHA-256 checksum and continues without requiring a terminal restart. An existing Python of another version can remain installed.

Using the command line tool included with Simplicity Installer, setup installs missing Simplicity SDK **2026.6.2**, Channel Sounding Analyzer **1.2.2**, ARM GCC **14.2.rel1**, Simplicity Commander, CMake, and Ninja packages. Large SDK downloads can take several minutes. If Silicon Labs requires a sign-in or package access, setup prints the affected packages; open Simplicity Installer to complete that step, then run setup again.

Setup also creates or repairs the project's local Python environment, installs and checks the Python dependencies, installs the Python, Pylance, Python Debugger, C/C++, and Genie Files extensions, refreshes SDK and compiler paths, and opens [genie_testing.code-workspace](genie_testing.code-workspace). Run setup again after moving the project or pulling updates. It reuses the tools and extensions already installed. These installation scripts support Windows.

**Genie Files** opens first in its own sidebar tab (the folder icon). The full project is available in the collapsed **genie_testing (project files)** folder at the bottom of the same tree. The regular Explorer remains available from its usual icon. **Outline** and **Timeline** start hidden. To restore this layout, run **Genie Files: Restore Testing Layout** from the command palette.

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
- Enter each board's clockwise yaw from its zero direction, viewed from above: `0`, `45`, `90`, through `315`.
- Angles are saved from 0 to 359 degrees; entering `-45` saves `315`, and `360` saves `0`.
- Type `skip` for an unknown condition.
- Reuse the saved heights when they stay the same.

Close the Genie graph or press **Ctrl+C** to stop and export the collected readings. A loss of new readings also stops the trial and saves its partial results.

#### Guided trials

Guided collection works in isolated calibration mode and normal dual-antenna mode.

The checklist has 40 conditions, covering 1, 2, 5, 10, and 20 m at eight orientations in 45-degree steps. Select the rotating board and enter the fixed setup once. You can jump to another distance or orientation and resume a compatible batch later.

Each completed trial uses the first 100 finite filtered distance estimates. Its raw capture retains the complete received stream. Interrupted trials save their collected readings and remain pending.

Guided batches live under [data/live/guided/](data/live/guided/), with names like `Batch1_ANT1-ANT1` or `Batch2_DUAL`. Expand a batch in Genie Files to see its `Recorded:` date and time. Each condition has a folder such as `1m_Ini0_Ref45`, containing its own recording time, workbook, plots, and capture files. Batch progress lives inside a saved condition's capture folder, for example `1m_Ini0_Ref0/capture/progress.json`. Retrying or deleting that condition through Genie Files preserves the rest of the batch's progress. Setup and guided collection automatically update older batch names and move their progress files.

To redo a condition, enter the same distance and board yaws. A completed retry replaces that condition's previous result and sends the old folder to the Recycle Bin. Interrupted retries keep the previous completed result and save their partial capture under `Interrupted captures`.

Delete a batch with **Genie Files > Guided batches > right-click the batch > Delete**, or enter `delete NUMBER` in the guided batch menu. This removes its captures and saved progress together. Stop an active guided session before deleting its batch. Saved board identities, antenna mode, offsets, and setup distinguish compatible batches.

### 5. Analyze the trials

Run [linearreg.py](linearreg.py). Each guided batch appears as one row; click it once to analyze all its completed trials. For single trials, use Ctrl or Shift to select the rows to analyze together, then click **Analyze selected**.

```powershell
.\.venv\Scripts\python.exe linearreg.py
```

For a full calibration batch, click the batch containing the 40 completed trials for one physical antenna and one offset baseline. The tool combines repeated trials within an orientation, then takes the median of the orientation results separately at each actual distance. It fits the five summarized distance points. Normal-mode analysis is optional: it checks remaining error and does not change calibration offsets.

Repeated trials at one distance provide a constant-bias estimate with an assumed slope of 1. Multiple distances let the fit estimate slope and intercept.

Single trials recorded through the Analyzer launcher, and guided trials, save their antenna mode and offsets with the capture. Regression uses those saved settings automatically, including after you change the current project configuration. ANT1 or ANT2 captures produce isolated-antenna recommendations; normal dual-antenna captures open an accuracy check. Select trials with the same configuration together. Older captures and replayed logs without saved settings still require manual confirmation.

These settings come from the project configuration when recording starts. Flash both boards after changes; the readings do not identify which firmware was actually flashed.

Apply physical calibration recommendations to the headers, flash both boards, and collect fresh measurements to check the result. After calibrating ANT1 and ANT2, run [normal.py](normal.py), flash both boards, and proceed with dual-antenna testing.

### Normal test results

Setup creates each teammate's empty master workbook with the shared headers, formatting and checklist. Readings and evidence fill only their local workbook. Pulling updates refreshes the report layout from their own saved captures.

Run [results.py](results.py) from Genie Files before testing. **Next test setup** saves the location, object in the way, board tilt, movement, power source and operator for future captures. Guided batches keep their original setup. Choose a baseline, accuracy or energy profile to compare Analyzer tracking or scheduling settings on the next connection; use a new batch for each profile. Calibration offsets stay fixed. Analyzer prints the saved setup before recording; unknown details stay labeled unknown. Saved settings are not flashed-firmware readback.

Collect the 40-condition baseline, then repeat comparable tests in open LOS, garage LOS and obstructed conditions. Test motion separately. Automatic single captures now stop at 100 finite filtered readings. Automatic captures also stop after 120 seconds or 1,000 result messages, saving partial results when readings remain unusable.

Results opens with real trials visible and batches collapsed. Expand a batch to select a trial. Use **Show** to include simulated captures, or search for a batch or condition. Distances and angles sort numerically. The selected trial shows which attempt, timing, power and TX evidence is available. **Label guide** gives short explanations.

**Master results.xlsx** in Genie Files refreshes and opens the local comparison workbook. Captures also refresh it automatically. **Overview** summarizes each batch/setup separately; its error statistics use all retained readings with a known actual distance, including partial recordings. **Definitions** explains every column and the common abbreviations. Other tabs contain individual trials, the 40-condition normal checklist, and saved configuration. Close Excel before refreshing. Use **Trial details** in [results.py](results.py) for corrections and evidence; edits made directly in the generated workbook are replaced on refresh. If Excel locks the file, the Results list still updates; close Excel and refresh to update the workbook.

Results without a finite filtered distance are counted from the complete raw recording, including Stop's final messages. Analyzer state messages are saved separately as diagnostics. Neither proves a complete count of radio attempts. **Import attempts** accepts a verified CSV with `attempt_id,status` (`valid`, `failed`, `unresolved`). Confirm complete coverage only if every attempt in the trial window was observed. Otherwise the valid percentage stays blank. Optional `request_time_s,result_time_s,clock_id` provide paired-clock latency; document the endpoints and source.

**Import reflector power** accepts reflector-only instrument samples with `time_s,current_ma` and optional measured `voltage_v`. Current is time-weighted over that window. Energy per sounding cycle additionally needs the number of cycles inside the same window. Unknown timing, current and verified TX power remain blank. The board supports at most +10 dBm; requested Analyzer settings are saved but are not RF-power readback.

The workbook lists the team's provisional targets. Confirm their definitions with Genie before using them as acceptance criteria. Normal-mode regression checks residual error; it does not recalibrate from validation data.

## Find the results

Single trials appear directly in [data/live/](data/live/) with names such as `1m_Ini0_Ref45`. Repeated single trials get suffixes such as `_02` and `_03`, keeping every capture. Unknown distances or angles stay labeled `Unknown`. Older single-trial folders are renamed automatically during setup or collection. Expand a trial in Genie Files to see its saved recording time and reports. Guided trials use the same condition names inside their batch under [data/live/guided/](data/live/guided/).

Naming rules, prompts, report formatting, and the Genie Files layout are shared code and update through Git. Recordings, workbooks, plots, guided progress, antenna mode, calibration offsets, board assignments, and laptop paths stay local. Run [setup.cmd](setup.cmd) after pulling updates to refresh shared editor defaults and the Genie Files extension while preserving your saved calibration and captures.

| Output | Purpose |
| --- | --- |
| `measurements.xlsx` | Measurements and trial conditions |
| `readings_in_order.png` | Reading sequence and actual-distance reference |
| `filtered_distance_distribution.png` | Distribution and actual-distance reference |
| `capture/raw.jsonl` | Original received records |
| `capture/measurements.csv` | Exported measurement rows |
| `capture/summary.json` | Trial conditions, statistics, and completion information |
| `capture/analyzer_telemetry.jsonl` | Requested Analyzer configuration and observed diagnostics, for new automatic captures |
| `data/results/master_results.xlsx` | Local trial comparisons, configuration, checklist and provisional targets |

The PNGs are saved at 300 dpi. A supplied actual distance appears as a red dotted line. Reading order uses sample number.

Deleting a trial folder removes its contained raw capture and reports. The Genie Files right-click **Delete** command sends live entries to the Windows Recycle Bin.
Hold **Ctrl** while clicking to select several live entries, or use **Shift** to select a range. Right-click a selected entry and choose **Delete** to move the selection together. Selecting a folder includes its contents.

Finalized source recordings are automatically recycled after their contents have been verified against the saved raw capture and reports have been exported. Temporary `.crswap` files can contain readings from an interrupted recording and require a duplicate check before cleanup.

## Share with teammates

Send the [GitHub repository link](https://github.com/DinyoFifty/genie_testing). Each teammate chooses **Code > Download ZIP**, extracts the entire folder, and follows **First-time setup**.

GitHub generates its ZIP from the files on the selected branch. Commit and push shared code changes to update future downloads; a separate ZIP in Downloads does not need to be rebuilt or uploaded.

Captures, trial progress, reports, logs, Python environments, build caches, board assignments, and local configuration files are excluded from Git and its ZIP download. Teammates get the shared defaults and calibrate their own boards. Share selected trial folders separately when needed.
