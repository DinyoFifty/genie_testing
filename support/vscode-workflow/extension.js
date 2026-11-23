const vscode = require("vscode");
const path = require("node:path");
const fs = require("node:fs/promises");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const scripts = ["calibration.py", "normal.py", "flash.py", "analyzer.py", "linearreg.py", "results.py", "README.md"];
const offsetHeader = "sl_rail_util_cs_antenna_offset_config.h";

async function activate(context) {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) return;
    const root = workspace.uri.fsPath;
    try {
        await Promise.all(["setup.cmd", "analyzer.py"].map(name => fs.access(path.join(root, name))));
    } catch { return; }
    await vscode.commands.executeCommand("setContext", "genieFilesManager.project", true);
    const liveRoot = path.join(root, "data", "live");
    const guidedRoot = path.join(liveRoot, "guided");
    const changes = new vscode.EventEmitter();

    async function openResults() {
        try {
            const python = path.join(root, ".venv", "Scripts", "python.exe");
            await fs.access(python);
            await promisify(execFile)(python, [path.join(root, "results.py"), "--refresh"],
                { cwd: root, windowsHide: true });
            await vscode.env.openExternal(vscode.Uri.file(path.join(root, "data", "results", "master_results.xlsx")));
        } catch (error) {
            await vscode.window.showErrorMessage(`Could not refresh master results. Close Excel and retry; run setup.cmd if Python is missing. ${error.message}`);
        }
    }

    async function prepareLayout() {
        // Use VS Code's view commands so its own layout persistence stays in charge.
        await vscode.commands.executeCommand("workbench.view.extension.genieFiles");
        await vscode.commands.executeCommand("genie.files.manager.open", { preserveFocus: true });
        const available = await vscode.commands.getCommands(true);
        for (const view of ["outline", "timeline"]) {
            if (available.includes(`${view}.removeView`)) {
                await vscode.commands.executeCommand(`${view}.removeView`);
            }
        }
        await vscode.workspace.getConfiguration("explorer", workspace.uri).update(
            "autoReveal", false, vscode.ConfigurationTarget.Workspace);
        await context.workspaceState.update("testingLayoutVersion", 2);
    }

    function isLiveEntry(filePath, base = liveRoot) {
        const relative = path.relative(base, filePath);
        return relative.length > 0 && relative !== ".."
            && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
    }

    async function batchProgress(batch) {
        const candidates = [path.join(batch, ".progress.json"), path.join(batch, "progress.json"),
            path.join(batch, "capture", "progress.json")];
        for (const entry of await fs.readdir(batch, { withFileTypes: true })) {
            if (entry.isDirectory() && !entry.name.startsWith(".") && !entry.name.startsWith("_")) {
                candidates.push(path.join(batch, entry.name, "capture", "progress.json"));
            }
        }
        const found = [];
        for (const candidate of candidates) {
            try { await fs.access(candidate); found.push(candidate); }
            catch (error) { if (error.code !== "ENOENT") throw error; }
        }
        if (found.length > 1) throw new Error("Multiple batch progress files exist; none was replaced.");
        return found[0];
    }

    async function placeBatchProgress(batch, source) {
        const progress = JSON.parse(await fs.readFile(source, "utf8"));
        const entries = await fs.readdir(batch, { withFileTypes: true });
        entries.sort((first, second) => first.name.localeCompare(second.name, undefined, { numeric: true }));
        for (const entry of entries) {
            if (!entry.isDirectory() || entry.name.startsWith(".") || entry.name.startsWith("_")) continue;
            const capture = path.join(batch, entry.name, "capture");
            let summary;
            try { summary = JSON.parse(await fs.readFile(path.join(capture, "summary.json"), "utf8")); }
            catch { continue; }
            if (summary.calibration?.batch_id !== progress.batch_id) continue;
            const destination = path.join(capture, "progress.json");
            if (source !== destination) {
                await vscode.workspace.fs.rename(vscode.Uri.file(source), vscode.Uri.file(destination), { overwrite: false });
            }
            return;
        }
    }

    function file(relativePath, label = path.basename(relativePath), branch = "testing") {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
        item.id = `${branch}:file:${path.normalize(relativePath)}`;
        item.resourceUri = vscode.Uri.file(path.join(root, relativePath));
        item.tooltip = item.resourceUri.fsPath;
        if (relativePath === "results.py") item.tooltip = "Review batches, test setup, measurement evidence, and the master workbook.";
        if (relativePath === "linearreg.py") item.tooltip = "Calibrate isolated antennas or optionally check normal-mode accuracy.";
        const managed = ["progress.json", "progress.lock", "progress.active.json"].includes(path.basename(relativePath).replace(/^\./, ""));
        item.contextValue = isLiveEntry(item.resourceUri.fsPath) && !managed ? "genieLiveFile" : "genieFile";
        item.command = { command: "vscode.open", title: "Open", arguments: [item.resourceUri] };
        return item;
    }

    function offsets(label, project) {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed);
        item.id = `offsets:${project}`;
        item.iconPath = new vscode.ThemeIcon("folder");
        item.offsetPath = path.join("firmware", project, "config", offsetHeader);
        return item;
    }

    function folder(relativePath, label = path.basename(relativePath), branch = "testing") {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed);
        item.id = `${branch}:folder:${path.normalize(relativePath)}`;
        item.branch = branch;
        item.iconPath = new vscode.ThemeIcon("folder");
        item.resourceUri = vscode.Uri.file(path.join(root, relativePath));
        item.relativePath = relativePath;
        item.contextValue = isLiveEntry(item.resourceUri.fsPath) && item.resourceUri.fsPath !== guidedRoot
            ? "genieLiveFolder" : "genieFolder";
        return item;
    }

    const provider = {
        onDidChangeTreeData: changes.event,
        getTreeItem: item => item,
        async getChildren(item) {
            if (item?.offsetPath) return [file(item.offsetPath, offsetHeader, item.id)];
            if (item?.relativePath === "data" && item.branch === "testing") {
                return [folder(path.join("data", "live"))];
            }
            if (item?.relativePath) {
                let entries;
                try {
                    entries = await vscode.workspace.fs.readDirectory(item.resourceUri);
                } catch (error) {
                    if (error.code === "FileNotFound") return [];
                    throw error;
                }
                entries.sort((first, second) => {
                    if (item.resourceUri.fsPath === liveRoot && (first[0] === "guided" || second[0] === "guided")) {
                        return Number(second[0] === "guided") - Number(first[0] === "guided");
                    }
                    const firstIsFolder = Boolean(first[1] & vscode.FileType.Directory);
                    const secondIsFolder = Boolean(second[1] & vscode.FileType.Directory);
                    const directoryOrder = Number(secondIsFolder) - Number(firstIsFolder);
                    return directoryOrder || first[0].localeCompare(second[0], undefined, { numeric: true });
                });
                entries = entries.filter(([name]) => !name.startsWith(".")
                    && !["progress.lock", "progress.active.json", "__pycache__"].includes(name)
                    && !name.endsWith(".code-workspace"));
                const children = entries.map(([name, type]) => {
                    const relativePath = path.join(item.relativePath, name);
                    const label = name === "guided" ? "Guided batches" : name === "_incomplete" ? "Interrupted captures" : name;
                    return type & vscode.FileType.Directory ? folder(relativePath, label, item.branch)
                        : file(relativePath, name, item.branch);
                });
                if (item.resourceUri.fsPath === liveRoot && !entries.some(([name]) => name === "guided")) {
                    children.unshift(folder(path.join(item.relativePath, "guided"), "Guided batches", item.branch));
                }
                try {
                    const batch = path.dirname(item.resourceUri.fsPath) === guidedRoot;
                    const summary = batch ? await batchProgress(item.resourceUri.fsPath)
                        : path.join(item.resourceUri.fsPath, "capture", "summary.json");
                    const metadata = JSON.parse(await fs.readFile(summary, "utf8"));
                    let recorded = batch ? metadata.created_at_utc
                        : metadata.session_started_at_utc || metadata.calibration?.started_at_utc;
                    let timeLabel = "Recorded";
                    if (!recorded && !batch) {
                        // Older captures saved host read times, but no session start timestamp.
                        const csv = await fs.readFile(path.join(item.resourceUri.fsPath, "capture", "measurements.csv"), "utf8");
                        const [header, firstRow] = csv.split(/\r?\n/, 2);
                        if (header.startsWith("source_line,read_at_utc,")) {
                            recorded = firstRow?.split(",")[1];
                            timeLabel = "First reading";
                        }
                    }
                    if (recorded && Number.isFinite(Date.parse(recorded))) {
                        const detail = new vscode.TreeItem(`${timeLabel}: ${new Date(recorded).toLocaleString("en-US")}`,
                            vscode.TreeItemCollapsibleState.None);
                        detail.id = `${item.id}:recorded`;
                        detail.iconPath = new vscode.ThemeIcon("clock");
                        detail.tooltip = recorded;
                        detail.command = { command: "vscode.open", title: batch ? "Batch details" : "Trial details",
                            arguments: [vscode.Uri.file(summary)] };
                        children.unshift(detail);
                    }
                } catch { /* Containers do not have a capture summary. */ }
                return children;
            }
            if (item) return [];
            const master = file(path.join("data", "results", "master_results.xlsx"), "Master results.xlsx");
            master.command = { command: "genieFilesManager.openResults", title: "Refresh and open master results" };
            return [
                offsets("Initiator offsets", "bt_cs_ncp"),
                offsets("Reflector offsets", "bt_cs_soc_reflector_studio"),
                ...scripts.map(name => file(name)),
                master,
                folder("data"),
                folder(".", `${path.basename(root)} (project files)`, "project"),
            ];
        },
    };

    const liveChanges = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(workspace, "data/**"));
    let refreshTimer;
    function scheduleRefresh() {
        if (refreshTimer) clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => {
            refreshTimer = undefined;
            changes.fire();
        }, 150);
    }
    context.subscriptions.push(
        changes,
        liveChanges,
        vscode.window.createTreeView("genie.files.manager", {
            treeDataProvider: provider,
            canSelectMany: true,
        }),
        vscode.commands.registerCommand("genieFilesManager.prepareLayout", prepareLayout),
        vscode.commands.registerCommand("genieFilesManager.openResults", openResults),
        vscode.commands.registerCommand("genieFilesManager.reveal", item =>
            vscode.commands.executeCommand("revealInExplorer", item.resourceUri)),
        vscode.commands.registerCommand("genieFilesManager.delete", async (item, selectedItems = [item]) => {
            if (!selectedItems.length) return;
            const uris = selectedItems.map(selected => selected?.resourceUri);
            if (uris.some(uri => !uri || !isLiveEntry(uri.fsPath) || uri.fsPath === guidedRoot
                || ["progress.json", "progress.lock", "progress.active.json"].includes(path.basename(uri.fsPath).replace(/^\./, "")))) {
                await vscode.window.showErrorMessage("Select files or folders inside data/live.");
                return;
            }
            try {
                const base = await fs.realpath(liveRoot);
                const resolved = await Promise.all(uris.map(uri => fs.realpath(uri.fsPath)));
                if (resolved.some(target => !isLiveEntry(target, base))) {
                    await vscode.window.showErrorMessage("Select files or folders inside data/live.");
                    return;
                }
                for (const uri of uris) {
                    const info = await fs.stat(uri.fsPath);
                    const start = info.isDirectory() ? uri.fsPath : path.dirname(uri.fsPath);
                    for (let current = start; isLiveEntry(current); current = path.dirname(current)) {
                        for (const markerPath of [path.join(current, ".progress.active.json"),
                            path.join(current, "progress.active.json"), path.join(current, "capture", "progress.active.json")]) {
                            try {
                                const marker = JSON.parse(await fs.readFile(markerPath, "utf8"));
                                if (!Number.isInteger(marker.pid) || marker.pid <= 0) throw new Error("Invalid active batch marker");
                                try { process.kill(marker.pid, 0); }
                                catch (error) { if (error.code === "ESRCH") continue; throw error; }
                                await vscode.window.showErrorMessage("Stop this guided session before deleting its batch or trials.");
                                return;
                            } catch (error) {
                                if (error.code !== "ENOENT") throw error;
                            }
                        }
                    }
                }
                const unique = [...new Map(uris.map(uri => [uri.fsPath, uri])).values()];
                const targets = unique.filter(uri =>
                    !unique.some(parent => isLiveEntry(uri.fsPath, parent.fsPath)));
                if (vscode.workspace.getConfiguration("explorer").get("confirmDelete", true)) {
                    const description = targets.length === 1
                        ? `'${path.basename(targets[0].fsPath)}'` : `${targets.length} selected items`;
                    const answer = await vscode.window.showWarningMessage(
                        `Move ${description} to the Recycle Bin?`,
                        { modal: true }, "Delete");
                    if (answer !== "Delete") return;
                }
                const retainedProgress = [];
                const batches = new Set(targets.filter(uri => isLiveEntry(uri.fsPath, guidedRoot)).map(uri =>
                    path.join(guidedRoot, path.relative(guidedRoot, uri.fsPath).split(path.sep)[0])));
                for (const batch of batches) {
                    if (targets.some(uri => uri.fsPath === batch)) continue;
                    const progress = await batchProgress(batch);
                    if (!progress || !targets.some(uri => isLiveEntry(progress, uri.fsPath))) continue;
                    const destination = path.join(batch, ".progress.json");
                    await vscode.workspace.fs.rename(vscode.Uri.file(progress), vscode.Uri.file(destination), { overwrite: false });
                    retainedProgress.push([batch, destination]);
                }
                const failures = [];
                for (const uri of targets) {
                    try {
                        await vscode.workspace.fs.delete(uri, { recursive: true, useTrash: true });
                    } catch (error) {
                        failures.push(`${path.basename(uri.fsPath)} (${error.message})`);
                    }
                }
                for (const [batch, progress] of retainedProgress) await placeBatchProgress(batch, progress);
                changes.fire();
                if (failures.length) {
                    await vscode.window.showErrorMessage(
                        `Could not move these items to the Recycle Bin. ${failures.join(", ")}`);
                }
            } catch (error) {
                await vscode.window.showErrorMessage(`Could not move the selected items to the Recycle Bin. ${error.message}`);
            }
        }),
        liveChanges.onDidCreate(scheduleRefresh),
        liveChanges.onDidChange(uri => {
            if (["summary.json", "progress.json", ".progress.json"].includes(path.basename(uri.fsPath))) scheduleRefresh();
        }),
        liveChanges.onDidDelete(scheduleRefresh),
        vscode.workspace.onDidCreateFiles(scheduleRefresh),
        vscode.workspace.onDidRenameFiles(scheduleRefresh),
        vscode.workspace.onDidDeleteFiles(scheduleRefresh),
        { dispose: () => clearTimeout(refreshTimer) },
    );
    // Opening our own view during activation would wait on this activation promise.
    const startup = setTimeout(async () => {
        try {
            if (context.workspaceState.get("testingLayoutVersion") !== 2) {
                await prepareLayout();
            } else {
                await vscode.commands.executeCommand("genie.files.manager.open", { preserveFocus: true });
            }
        } catch (error) {
            await vscode.window.showWarningMessage(
                `Genie Files could not arrange the sidebar. Run "Genie Files: Restore Testing Layout" to retry. ${error.message}`);
        }
    }, 0);
    context.subscriptions.push({ dispose: () => clearTimeout(startup) });
}

module.exports = { activate };
