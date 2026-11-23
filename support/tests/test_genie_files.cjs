// Check the sidebar against real temporary folders, without opening VS Code.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "genie-sidebar-test-"));
    const live = path.join(root, "data", "live");
    const batch = path.join(live, "guided", "Trial1_ANT1-ANT1_2026-11-22_3-44PM");
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
    let provider;
    const disposable = { dispose() {} };
    const events = { onDidCreate: () => disposable, onDidChange: () => disposable, onDidDelete: () => disposable };
    const vscode = {
        TreeItem: class { constructor(label) { this.label = label; } },
        TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
        EventEmitter: class { constructor() { this.event = () => disposable; } fire() {} },
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
                    assert.equal(savedState.get("testingLayoutVersion"), 1, "Save completion before reloading to avoid a loop");
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
        await fs.writeFile(path.join(trial, "capture", "summary.json"), JSON.stringify({ session_started_at_utc: recorded }));
        await fs.writeFile(path.join(trial, "measurements.xlsx"), "fixture");
        await fs.writeFile(path.join(batch, "progress.json"), "{}");
        await fs.writeFile(path.join(batch, "progress.lock"), "lock");
        const source = await fs.readFile(path.join(__dirname, "../vscode-workflow/extension.js"), "utf8");
        const context = vm.createContext({
            require: name => name === "vscode" ? vscode : require(name), process, module: { exports: {} },
            setTimeout: callback => { startupTasks.push(callback); return callback; },
            clearTimeout() {},
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
        assert.deepEqual(visibleViews, ["genie.files.manager", "workbench.explorer.openEditorsView",
            "workbench.explorer.fileView", "fixture.other"]);
        assert.equal(collapsed, true);
        assert.equal(autoReveal, false);
        assert.equal(savedState.get("testingLayoutVersion"), 1);
        assert.equal(executed.filter(([name]) => name === "workbench.action.reloadWindow").length, 1);

        // Opening the workspace again shows Genie Files without undoing a teammate's changes.
        visibleViews.push("outline");
        collapsed = false;
        executed.length = 0;
        await activate();
        assert.equal(collapsed, true);
        assert(visibleViews.includes("outline"));
        assert(executed.some(([name]) => name === "genie.files.manager.open"));
        assert(!executed.some(([name]) => name.endsWith(".removeView") || name === "views.moveViewUp"
            || name === "workbench.action.reloadWindow"));
        await commands.get("genieFilesManager.prepareLayout")();
        assert(!visibleViews.includes("outline"));
        const data = (await provider.getChildren()).find(item => item.label === "data");
        const liveItem = (await provider.getChildren(data))[0];
        const liveChildren = await provider.getChildren(liveItem);
        assert.equal(liveChildren[0].label, "Guided batches");
        assert.equal(liveChildren[1].label, "2026-11-22_3-40PM");
        const batchItem = (await provider.getChildren(liveChildren[0]))[0];
        const contents = await provider.getChildren(batchItem);
        assert(!contents.some(item => item.label === "progress.lock"));
        const condition = contents.find(item => item.label === "1m_Ini0_Ref45");
        const conditionContents = await provider.getChildren(condition);
        assert(conditionContents[0].label.startsWith("Recorded: "));
        assert.equal(conditionContents[0].tooltip, recorded);
        assert(conditionContents.some(item => item.label === "capture"));
        assert(conditionContents.some(item => item.label === "measurements.xlsx"));

        const remove = commands.get("genieFilesManager.delete");
        const marker = path.join(batch, "progress.active.json");
        await fs.writeFile(marker, JSON.stringify({ pid: process.pid }));
        await remove(condition, [condition]);
        assert.equal(deleted.length, 0);
        assert(errors.at(-1).includes("Stop this guided session"));
        await fs.unlink(marker);
        const workbook = conditionContents.find(item => item.label === "measurements.xlsx");
        await remove(workbook, [workbook]);
        assert.equal(deleted.length, 1);
        await remove(batchItem, [batchItem]);
        assert.equal(deleted.length, 2);
        await assert.rejects(fs.stat(batch), { code: "ENOENT" });
        assert((await fs.stat(path.join(live, "2026-11-22_3-40PM"))).isDirectory());
        await remove(liveChildren[0], [liveChildren[0]]);
        assert.equal(deleted.length, 2);
        // A failed arrangement remains retryable and does not prevent the file tree from registering.
        savedState.clear();
        failCommand = "views.moveViewUp";
        await activate();
        assert.equal(savedState.get("testingLayoutVersion"), undefined);
        failCommand = undefined;
        await commands.get("genieFilesManager.prepareLayout")();
        assert.equal(savedState.get("testingLayoutVersion"), 1);

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
