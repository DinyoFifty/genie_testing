// Check the sidebar against real temporary folders, without opening VS Code.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
    const manifest = JSON.parse(await fs.readFile(path.join(__dirname, "../vscode-workflow/package.json"), "utf8"));
    assert(!manifest.contributes.views.explorer, "Keep Genie Files out of the resizable Explorer stack");
    assert.equal(manifest.contributes.views.genieFiles[0].id, "genie.files.manager");
    assert.equal(manifest.contributes.viewsContainers.activitybar[0].id, "genieFiles");
    assert(manifest.activationEvents.includes("workspaceContains:setup.cmd"));
    assert(!manifest.activationEvents.includes("onStartupFinished"), "The file list must not wait for delayed startup");
    assert(!manifest.activationEvents.includes("*"), "Only activate in a matching project or when its view is requested");
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "genie-sidebar-test-"));
    const live = path.join(root, "data", "live");
    const batch = path.join(live, "guided", "Batch1_ANT1-ANT1");
    const trial = path.join(batch, "1m_Ini0_Ref45");
    const recorded = "2026-11-22T20:44:17Z";
    const commands = new Map();
    const errors = [];
    const deleted = [];
    const executed = [];
    const savedState = new Map();
    const viewIds = ["workbench.explorer.openEditorsView", "workbench.explorer.fileView", "outline", "timeline",
        "fixture.other", "genie.files.manager"];
    let visibleViews = [...viewIds];
    const viewOrder = [...viewIds];
    let focusedView;
    let collapsed = false;
    let autoReveal = true;
    let failCommand;
    let activationReturned = false;
    const startupTasks = [];
    const watcherHandlers = new Map();
    let refreshes = 0;
    let provider;
    let directoryReads = 0;
    const disposable = { dispose() {} };
    const events = Object.fromEntries(["Create", "Change", "Delete"].map(name =>
        [`onDid${name}`, callback => { watcherHandlers.set(name, callback); return disposable; }]));
    const vscode = {
        TreeItem: class { constructor(label) { this.label = label; } },
        TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
        EventEmitter: class { constructor() { this.event = () => disposable; } fire() { refreshes++; } },
        ThemeIcon: class {}, RelativePattern: class {}, Uri: { file: fsPath => ({ fsPath }) },
        FileType: { File: 1, Directory: 2 },
        ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
        workspace: {
            workspaceFolders: [{ uri: { fsPath: root } }],
            createFileSystemWatcher: () => events,
            getConfiguration: () => ({
                get: () => true,
                async update(name, value, target) {
                    assert.equal(name, "autoReveal");
                    if (target === vscode.ConfigurationTarget.WorkspaceFolder) {
                        throw new Error("explorer.autoReveal does not support the folder resource scope");
                    }
                    assert.equal(target, vscode.ConfigurationTarget.Workspace);
                    autoReveal = value;
                },
            }),
            onDidCreateFiles: () => disposable, onDidRenameFiles: () => disposable, onDidDeleteFiles: () => disposable,
            fs: {
                async readDirectory(uri) {
                    directoryReads++;
                    const entries = await fs.readdir(uri.fsPath, { withFileTypes: true });
                    return entries.map(entry => [entry.name, entry.isDirectory() ? 2 : 1]);
                },
                async delete(uri, options) {
                    assert.equal(options.useTrash, true);
                    const relative = path.relative(root, uri.fsPath);
                    assert(relative && !relative.startsWith("..") && !path.isAbsolute(relative));
                    deleted.push(uri.fsPath);
                    await fs.rm(uri.fsPath, { recursive: true });
                },
                async rename(source, destination, options) {
                    assert.equal(options.overwrite, false);
                    assert(path.relative(root, destination.fsPath) && !path.relative(root, destination.fsPath).startsWith(".."));
                    try { await fs.access(destination.fsPath); throw Object.assign(new Error("exists"), { code: "EEXIST" }); }
                    catch (error) { if (error.code !== "ENOENT") throw error; }
                    await fs.rename(source.fsPath, destination.fsPath);
                },
            },
        },
        window: {
            createTreeView(name, options) { provider = options.treeDataProvider; return disposable; },
            async showErrorMessage(message) { errors.push(message); },
            async showWarningMessage() { return "Delete"; },
        },
        commands: {
            registerCommand(name, callback) { commands.set(name, callback); return disposable; },
            async getCommands() { return viewIds.map(id => `${id}.removeView`); },
            async executeCommand(name, ...args) {
                executed.push([name, ...args]);
                if (name === "genie.files.manager.open") {
                    assert(activationReturned, "Opening our own view must wait until activation has returned");
                }
                if (name === failCommand) throw new Error("fixture command failure");
                if (name.endsWith(".removeView")) {
                    visibleViews = visibleViews.filter(id => id !== name.slice(0, -".removeView".length));
                } else if (name === "genie.files.manager.focus") {
                    focusedView = "genie.files.manager";
                } else if (name === "views.moveViewUp") {
                    // VS Code saves the order here; existing panes stay put until reopened.
                    const ordered = viewOrder.filter(id => visibleViews.includes(id));
                    const index = ordered.indexOf(focusedView);
                    assert(index >= 0, "Moving views requires focus on Genie Files");
                    if (index > 0) {
                        const from = viewOrder.indexOf(focusedView);
                        const to = viewOrder.indexOf(ordered[index - 1]);
                        [viewOrder[to], viewOrder[from]] = [viewOrder[from], viewOrder[to]];
                    }
                } else if (name === "workbench.files.action.collapseExplorerFolders") {
                    collapsed = true;
                } else if (name === "workbench.action.reloadWindow") {
                    assert.fail("Arranging the sidebar must not reload the user's workspace");
                    visibleViews = viewOrder.filter(id => visibleViews.includes(id));
                }
            },
        },
    };
    try {
        await fs.writeFile(path.join(root, "setup.cmd"), "fixture");
        await fs.writeFile(path.join(root, "analyzer.py"), "fixture");
        await fs.mkdir(path.join(trial, "capture"), { recursive: true });
        await fs.mkdir(path.join(live, "2026-11-22_3-40PM"));
        await fs.writeFile(path.join(trial, "capture", "summary.json"), JSON.stringify({ session_started_at_utc: recorded,
            calibration: { batch_id: "saved-batch" } }));
        await fs.writeFile(path.join(trial, "measurements.xlsx"), "fixture");
        const progressPath = path.join(trial, "capture", "progress.json");
        const progressBytes = JSON.stringify({ batch_id: "saved-batch", created_at_utc: recorded });
        await fs.writeFile(progressPath, progressBytes);
        await fs.writeFile(path.join(batch, ".progress.lock"), "lock");
        const source = await fs.readFile(path.join(__dirname, "../vscode-workflow/extension.js"), "utf8");
        const context = vm.createContext({
            require: name => name === "vscode" ? vscode : require(name), process, module: { exports: {} },
            setTimeout: callback => { startupTasks.push(callback); return callback; },
            clearTimeout(callback) {
                const index = startupTasks.indexOf(callback);
                if (index >= 0) startupTasks.splice(index, 1);
            },
        });
        vm.runInContext(source, context);
        const extensionContext = {
            subscriptions: [],
            workspaceState: { get: key => savedState.get(key), async update(key, value) { savedState.set(key, value); } },
        };
        async function activate() {
            activationReturned = false;
            await context.module.exports.activate(extensionContext);
            activationReturned = true;
            while (startupTasks.length) await startupTasks.shift()();
        }
        await activate();
        assert.equal(directoryReads, 0, "Register the sidebar without scanning recordings or firmware folders");
        assert.equal((await provider.getChildren()).length, 12);
        assert.equal(directoryReads, 0, "The initial file list must not need folder enumeration");
        assert(!visibleViews.includes("outline") && !visibleViews.includes("timeline"));
        assert(visibleViews.includes("workbench.explorer.fileView"), "Keep the regular Explorer available");
        assert(executed.some(([name]) => name === "workbench.view.extension.genieFiles"));
        assert.equal(collapsed, false);
        assert.equal(autoReveal, false);
        assert.equal(savedState.get("testingLayoutVersion"), 2);
        assert.equal(executed.filter(([name]) => name === "workbench.action.reloadWindow").length, 0);

        // Opening the workspace again shows Genie Files without undoing a teammate's changes.
        visibleViews.push("outline");
        collapsed = false;
        executed.length = 0;
        await activate();
        assert.equal(collapsed, false);
        assert(visibleViews.includes("outline"));
        assert(executed.some(([name]) => name === "genie.files.manager.open"));
        assert(!executed.some(([name]) => name.endsWith(".removeView") || name === "views.moveViewUp"
            || name === "workbench.action.reloadWindow"));
        await commands.get("genieFilesManager.prepareLayout")();
        assert(!visibleViews.includes("outline"));
        savedState.set("testingLayoutVersion", 1);
        await activate();
        assert.equal(savedState.get("testingLayoutVersion"), 2);
        assert(visibleViews.includes("workbench.explorer.fileView"));
        const data = (await provider.getChildren()).find(item => item.label === "data");
        const master = (await provider.getChildren()).find(item => item.label === "Master results.xlsx");
        assert.equal(master.command.command, "genieFilesManager.openResults");
        assert(commands.has("genieFilesManager.openResults"));
        assert((await provider.getChildren()).some(item => item.label === "results.py"));
        const project = (await provider.getChildren()).find(item => item.relativePath === ".");
        assert(project.label.endsWith(" (project files)"));
        const projectChildren = await provider.getChildren(project);
        const projectAnalyzer = projectChildren.find(item => item.label === "analyzer.py");
        const quickAnalyzer = (await provider.getChildren()).find(item => item.label === "analyzer.py");
        assert.equal(projectAnalyzer.resourceUri.fsPath, quickAnalyzer.resourceUri.fsPath);
        assert.notEqual(projectAnalyzer.id, quickAnalyzer.id, "The two shortcuts must have distinct tree identities");
        assert.equal((await provider.getChildren()).find(item => item.label === "analyzer.py").id, quickAnalyzer.id);
        const identities = new Set();
        async function checkIdentities(parent) {
            for (const child of await provider.getChildren(parent)) {
                assert(child.id && !identities.has(child.id), `Repeated tree identity: ${child.id}`);
                identities.add(child.id);
                if (child.collapsibleState !== 0 && (child.relativePath || child.offsetPath)) await checkIdentities(child);
            }
        }
        await checkIdentities();

        // Streaming samples and workbook writes should not rebuild the file tree.
        watcherHandlers.get("Change")({ fsPath: path.join(trial, "capture", "raw.jsonl") });
        watcherHandlers.get("Change")({ fsPath: path.join(root, "data", "results", "master_results.xlsx") });
        assert.equal(startupTasks.length, 0);
        const beforeRefresh = refreshes;
        watcherHandlers.get("Create")();
        watcherHandlers.get("Change")({ fsPath: path.join(trial, "capture", "summary.json") });
        watcherHandlers.get("Delete")();
        assert.equal(startupTasks.length, 1, "One refresh should cover a burst of changes");
        await startupTasks.shift()();
        assert.equal(refreshes, beforeRefresh + 1);
        const liveItem = (await provider.getChildren(data))[0];
        const liveChildren = await provider.getChildren(liveItem);
        assert.equal(liveChildren[0].label, "Guided batches");
        assert.equal(liveChildren[1].label, "2026-11-22_3-40PM");
        const batchItem = (await provider.getChildren(liveChildren[0]))[0];
        assert.equal(batchItem.label, "Batch1_ANT1-ANT1");
        const contents = await provider.getChildren(batchItem);
        assert.equal(contents[0].label, `Recorded: ${new Date(recorded).toLocaleString("en-US")}`);
        assert.equal(contents[0].tooltip, recorded);
        assert.equal(contents[0].command.arguments[0].fsPath, progressPath);
        assert(!contents.some(item => item.label === "progress.json" || item.label === "progress.lock"));
        assert(!contents.some(item => item.label === "capture"));
        const condition = contents.find(item => item.label === "1m_Ini0_Ref45");
        const conditionContents = await provider.getChildren(condition);
        assert(conditionContents[0].label.startsWith("Recorded: "));
        assert.equal(conditionContents[0].tooltip, recorded);
        assert(conditionContents.some(item => item.label === "capture"));
        assert(conditionContents.some(item => item.label === "measurements.xlsx"));
        const stateFiles = await provider.getChildren(conditionContents.find(item => item.label === "capture"));
        const progressItem = stateFiles.find(item => item.label === "progress.json");
        assert.equal(progressItem.contextValue, "genieFile");

        // Legacy single trials retain a visible time after their dated folder name changes.
        const single = liveChildren[1];
        await fs.mkdir(path.join(single.resourceUri.fsPath, "capture"));
        await fs.writeFile(path.join(single.resourceUri.fsPath, "capture", "summary.json"), "{}");
        await fs.writeFile(path.join(single.resourceUri.fsPath, "capture", "measurements.csv"),
            `source_line,read_at_utc,distanceMain\n1,${recorded},1.2\n`);
        const singleContents = await provider.getChildren(single);
        assert(singleContents[0].label.startsWith("First reading: "));
        assert.equal(singleContents[0].tooltip, recorded);

        const remove = commands.get("genieFilesManager.delete");
        await remove(progressItem, [progressItem]);
        assert.equal(deleted.length, 0);
        const marker = path.join(batch, ".progress.active.json");
        await fs.writeFile(marker, JSON.stringify({ pid: process.pid }));
        await remove(condition, [condition]);
        assert.equal(deleted.length, 0);
        assert(errors.at(-1).includes("Stop this guided session"));
        await fs.unlink(marker);
        const workbook = conditionContents.find(item => item.label === "measurements.xlsx");
        await remove(workbook, [workbook]);
        assert.equal(deleted.length, 1);

        // Deleting the condition holding progress preserves the rest of the batch.
        const otherTrial = path.join(batch, "2m_Ini0_Ref0");
        await fs.mkdir(path.join(otherTrial, "capture"), { recursive: true });
        await fs.writeFile(path.join(otherTrial, "capture", "summary.json"), JSON.stringify({
            session_started_at_utc: recorded, calibration: { batch_id: "saved-batch" } }));
        await remove(condition, [condition]);
        assert.equal(deleted.length, 2);
        const otherProgress = path.join(otherTrial, "capture", "progress.json");
        assert.equal(await fs.readFile(otherProgress, "utf8"), progressBytes);
        const otherItem = (await provider.getChildren(batchItem)).find(item => item.label === "2m_Ini0_Ref0");
        const otherCapture = (await provider.getChildren(otherItem)).find(item => item.label === "capture");
        await remove(otherCapture, [otherCapture]);
        assert.equal(deleted.length, 3);
        assert.equal(await fs.readFile(path.join(batch, ".progress.json"), "utf8"), progressBytes);
        assert((await provider.getChildren(batchItem))[0].label.startsWith("Recorded: "));
        await remove(batchItem, [batchItem]);
        assert.equal(deleted.length, 4);
        await assert.rejects(fs.stat(batch), { code: "ENOENT" });
        assert((await fs.stat(path.join(live, "2026-11-22_3-40PM"))).isDirectory());
        await remove(liveChildren[0], [liveChildren[0]]);
        assert.equal(deleted.length, 4);
        // A failed arrangement remains retryable and does not prevent the file tree from registering.
        savedState.clear();
        failCommand = "workbench.view.extension.genieFiles";
        await activate();
        assert.equal(savedState.get("testingLayoutVersion"), undefined);
        failCommand = undefined;
        await commands.get("genieFilesManager.prepareLayout")();
        assert.equal(savedState.get("testingLayoutVersion"), 2);

        // Installing the extension does not rearrange an unrelated project.
        vscode.workspace.workspaceFolders = [{ uri: { fsPath: path.join(root, "unrelated") } }];
        executed.length = 0;
        await activate();
        assert.equal(executed.length, 0);
        console.log("Sidebar layout, repeat opening, restore/retry, project scoping, grouping, and deletion protection passed");
    } finally {
        assert.equal(path.dirname(root), os.tmpdir());
        await fs.rm(root, { recursive: true, force: true });
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
