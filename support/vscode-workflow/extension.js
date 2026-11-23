const vscode = require("vscode");
const path = require("node:path");
const fs = require("node:fs/promises");

const scripts = ["calibration.py", "normal.py", "flash.py", "analyzer.py", "linearreg.py", "README.md"];
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

    async function prepareLayout() {
        // Use VS Code's view commands so its own layout persistence stays in charge.
        await vscode.commands.executeCommand("workbench.view.explorer");
        await vscode.commands.executeCommand("genie.files.manager.open", { preserveFocus: true });
        const available = await vscode.commands.getCommands(true);
        for (const view of ["outline", "timeline"]) {
            if (available.includes(`${view}.removeView`)) {
                await vscode.commands.executeCommand(`${view}.removeView`);
            }
        }
        await vscode.workspace.getConfiguration("explorer", workspace.uri).update(
            "autoReveal", false, vscode.ConfigurationTarget.Workspace);
        await vscode.commands.executeCommand("workbench.files.action.collapseExplorerFolders");
        // At most one move per registered view is enough to reach the top.
        const viewCount = available.filter(command => command.endsWith(".removeView")).length;
        for (let index = 0; index < viewCount; index++) {
            await vscode.commands.executeCommand("genie.files.manager.focus");
            await vscode.commands.executeCommand("views.moveViewUp");
        }
        await context.workspaceState.update("testingLayoutVersion", 1);
        // VS Code's move command saves the order; one refresh applies it to existing panes.
        await vscode.commands.executeCommand("workbench.action.reloadWindow");
    }

    function isLiveEntry(filePath, base = liveRoot) {
        const relative = path.relative(base, filePath);
        return relative.length > 0 && relative !== ".."
            && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
    }

    function file(relativePath, label = path.basename(relativePath)) {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
        item.resourceUri = vscode.Uri.file(path.join(root, relativePath));
        item.tooltip = item.resourceUri.fsPath;
        const managed = ["progress.json", "progress.lock", "progress.active.json"].includes(path.basename(relativePath));
        item.contextValue = isLiveEntry(item.resourceUri.fsPath) && !managed ? "genieLiveFile" : "genieFile";
        item.command = { command: "vscode.open", title: "Open", arguments: [item.resourceUri] };
        return item;
    }

    function offsets(label, project) {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed);
        item.iconPath = new vscode.ThemeIcon("folder");
        item.offsetPath = path.join("firmware", project, "config", offsetHeader);
        return item;
    }

    function folder(relativePath, label = path.basename(relativePath)) {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed);
        item.id = relativePath;
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
            if (item?.offsetPath) return [file(item.offsetPath)];
            if (item?.relativePath === "data") return [folder(path.join("data", "live"))];
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
                    && !["progress.lock", "progress.active.json"].includes(name));
                const children = entries.map(([name, type]) => {
                    const relativePath = path.join(item.relativePath, name);
                    const label = name === "guided" ? "Guided batches" : name === "_incomplete" ? "Interrupted captures" : name;
                    return type & vscode.FileType.Directory ? folder(relativePath, label) : file(relativePath);
                });
                if (item.resourceUri.fsPath === liveRoot && !entries.some(([name]) => name === "guided")) {
                    children.unshift(folder(path.join(item.relativePath, "guided"), "Guided batches"));
                }
                try {
                    const summary = path.join(item.resourceUri.fsPath, "capture", "summary.json");
                    const metadata = JSON.parse(await fs.readFile(summary, "utf8"));
                    const recorded = metadata.session_started_at_utc || metadata.calibration?.started_at_utc;
                    if (recorded) {
                        const detail = new vscode.TreeItem(`Recorded: ${new Date(recorded).toLocaleString()}`,
                            vscode.TreeItemCollapsibleState.None);
                        detail.iconPath = new vscode.ThemeIcon("clock");
                        detail.tooltip = recorded;
                        detail.command = { command: "vscode.open", title: "Trial details", arguments: [vscode.Uri.file(summary)] };
                        children.unshift(detail);
                    }
                } catch { /* Containers do not have a capture summary. */ }
                return children;
            }
            if (item) return [];
            return [
                offsets("Initiator offsets", "bt_cs_ncp"),
                offsets("Reflector offsets", "bt_cs_soc_reflector_studio"),
                ...scripts.map(name => file(name)),
                folder("data"),
            ];
        },
    };

    const liveChanges = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(workspace, "data/live/**"));
    context.subscriptions.push(
        changes,
        liveChanges,
        vscode.window.createTreeView("genie.files.manager", {
            treeDataProvider: provider,
            canSelectMany: true,
        }),
        vscode.commands.registerCommand("genieFilesManager.prepareLayout", prepareLayout),
        vscode.commands.registerCommand("genieFilesManager.reveal", item =>
            vscode.commands.executeCommand("revealInExplorer", item.resourceUri)),
        vscode.commands.registerCommand("genieFilesManager.delete", async (item, selectedItems = [item]) => {
            if (!selectedItems.length) return;
            const uris = selectedItems.map(selected => selected?.resourceUri);
            if (uris.some(uri => !uri || !isLiveEntry(uri.fsPath) || uri.fsPath === guidedRoot
                || ["progress.json", "progress.lock", "progress.active.json"].includes(path.basename(uri.fsPath)))) {
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
                        try {
                            const marker = JSON.parse(await fs.readFile(path.join(current, "progress.active.json"), "utf8"));
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
                const failures = [];
                for (const uri of targets) {
                    try {
                        await vscode.workspace.fs.delete(uri, { recursive: true, useTrash: true });
                    } catch (error) {
                        failures.push(`${path.basename(uri.fsPath)} (${error.message})`);
                    }
                }
                changes.fire();
                if (failures.length) {
                    await vscode.window.showErrorMessage(
                        `Could not move these items to the Recycle Bin. ${failures.join(", ")}`);
                }
            } catch (error) {
                await vscode.window.showErrorMessage(`Could not move the selected items to the Recycle Bin. ${error.message}`);
            }
        }),
        liveChanges.onDidCreate(() => changes.fire()),
        liveChanges.onDidChange(() => changes.fire()),
        liveChanges.onDidDelete(() => changes.fire()),
        vscode.workspace.onDidCreateFiles(() => changes.fire()),
        vscode.workspace.onDidRenameFiles(() => changes.fire()),
        vscode.workspace.onDidDeleteFiles(() => changes.fire()),
    );
    // Opening our own view during activation would wait on this activation promise.
    const startup = setTimeout(async () => {
        try {
            if (context.workspaceState.get("testingLayoutVersion") !== 1) {
                await prepareLayout();
            } else {
                await vscode.commands.executeCommand("genie.files.manager.open", { preserveFocus: true });
                await vscode.commands.executeCommand("workbench.files.action.collapseExplorerFolders");
            }
        } catch (error) {
            await vscode.window.showWarningMessage(
                `Genie Files could not arrange the sidebar. Run "Genie Files: Restore Testing Layout" to retry. ${error.message}`);
        }
    }, 0);
    context.subscriptions.push({ dispose: () => clearTimeout(startup) });
}

module.exports = { activate };
