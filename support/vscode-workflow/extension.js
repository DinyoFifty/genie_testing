const vscode = require("vscode");
const path = require("node:path");
const fs = require("node:fs/promises");

const scripts = ["calibration.py", "normal.py", "flash.py", "analyzer.py", "linearreg.py", "README.md"];
const offsetHeader = "sl_rail_util_cs_antenna_offset_config.h";

function activate(context) {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) return;
    const root = workspace.uri.fsPath;
    const liveRoot = path.join(root, "data", "live");
    const changes = new vscode.EventEmitter();

    function isLiveEntry(filePath, base = liveRoot) {
        const relative = path.relative(base, filePath);
        return relative.length > 0 && relative !== ".."
            && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
    }

    function file(relativePath, label = path.basename(relativePath)) {
        const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
        item.resourceUri = vscode.Uri.file(path.join(root, relativePath));
        item.tooltip = item.resourceUri.fsPath;
        item.contextValue = isLiveEntry(item.resourceUri.fsPath) ? "genieLiveFile" : "genieFile";
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
        item.contextValue = isLiveEntry(item.resourceUri.fsPath) ? "genieLiveFolder" : "genieFolder";
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
                    const firstIsFolder = Boolean(first[1] & vscode.FileType.Directory);
                    const secondIsFolder = Boolean(second[1] & vscode.FileType.Directory);
                    const directoryOrder = Number(secondIsFolder) - Number(firstIsFolder);
                    return directoryOrder || first[0].localeCompare(second[0], undefined, { numeric: true });
                });
                return entries.map(([name, type]) => {
                    const relativePath = path.join(item.relativePath, name);
                    return type & vscode.FileType.Directory ? folder(relativePath) : file(relativePath);
                });
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
        vscode.commands.registerCommand("genieFilesManager.reveal", item =>
            vscode.commands.executeCommand("revealInExplorer", item.resourceUri)),
        vscode.commands.registerCommand("genieFilesManager.delete", async (item, selectedItems = [item]) => {
            if (!selectedItems.length) return;
            const uris = selectedItems.map(selected => selected?.resourceUri);
            if (uris.some(uri => !uri || !isLiveEntry(uri.fsPath))) {
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
}

module.exports = { activate };
