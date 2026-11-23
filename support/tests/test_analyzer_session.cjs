// Hardware-free checks for the persistent Analyzer recorder protocol.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const { EventEmitter } = require("node:events");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "support", "genie_analyzer.cjs"), "utf8");
const definitions = source.slice(0, source.indexOf("\nmain().catch("));
const line = value => JSON.stringify({ initiatorId: "initiator", reflectorId: "reflector", distanceMain: value }) + "\n";

function checkAntennaModes() {
    for (const [session, count, override, expected] of [[true, 1, "2", 1], [true, 2, "1", 2], [false, 2, "1", 1]]) {
        const input = new PassThrough();
        const processMock = new EventEmitter();
        Object.assign(processMock, {
            argv: [process.execPath, "genie_analyzer.cjs", "profile", "recording", ...(session ? ["--session"] : [])],
            env: { GENIE_PBR_ANTENNAS: override }, stdin: input, execPath: process.execPath,
        });
        const filesystem = { ...fs, readFileSync: (file, ...args) => file.endsWith("sl_rail_util_cs_antenna_offset_config.h")
            ? `#define SL_RAIL_UTIL_CS_ANTENNA_COUNT ${count}\n` : fs.readFileSync(file, ...args) };
        const context = vm.createContext({
            require: name => name === "node:fs" ? filesystem : require(name),
            __dirname: path.join(root, "support"), process: processMock, Buffer, setTimeout, clearTimeout, console,
        });
        try {
            vm.runInContext(definitions, context);
            assert.equal(vm.runInContext("pbrAntennas", context), expected);
        } finally {
            input.destroy();
        }
    }
}

async function checkSession() {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "genie-recorder-test-"));
    const input = new PassThrough();
    const processMock = new EventEmitter();
    Object.assign(processMock, { argv: [process.execPath, "genie_analyzer.cjs", folder, folder, "--session"],
        env: {}, stdin: input, execPath: process.execPath });
    const events = [];
    const expected = [];
    let receive = () => {};
    let failure;
    let starts = 0;
    const send = data => {
        try { receive(data); } catch (error) { failure = error; }
    };
    const bytes = value => send({ bytes: Buffer.from(value).toString("base64") });
    const command = value => input.write(JSON.stringify(value) + "\n");
    const debuggerMock = {
        receive(callback) { receive = callback; },
        check() { if (failure) throw failure; },
        async evaluate(fn) {
            if (fn.toString().includes("const originalPicker")) {
                starts += 1;
                const contents = starts === 1 ? line(1) + line(null) + line("2") + line(2) : line(4);
                expected.push(contents + line(9));
                // Split a JSON line across chunks and put several lines in one chunk.
                bytes(contents.slice(0, 19));
                bytes(contents.slice(19));
            } else {
                bytes(line(9)); // Original results queued while Stop flushes.
                send({ close: true });
            }
            return true;
        },
    };
    const context = vm.createContext({ require, __dirname: path.join(root, "support"), process: processMock, Buffer,
        setTimeout, clearTimeout, console: { error() {}, log(value) {
            const event = JSON.parse(value);
            events.push(event);
            if (event.event === "connected") command({ command: "start", path: path.join(folder, "first.jsonl"), max_estimates: 2 });
            if (event.event === "stopped" && starts === 1) {
                setTimeout(() => command({ command: "start", path: path.join(folder, "second.jsonl"), max_estimates: 100 }), 5);
            }
            if (event.event === "recording" && starts === 2) command({ command: "stop" });
            if (event.event === "stopped" && starts === 2) {
                setTimeout(() => command({ command: "start", path: path.join(folder, "third.jsonl"), max_estimates: 100 }), 5);
            }
            if (event.event === "recording" && starts === 3) input.end();
        } } });
    context.debuggerMock = debuggerMock;
    try {
        vm.runInContext(definitions, context);
        await vm.runInContext('recordSession(debuggerMock, "record-button")', context);
        assert.deepEqual(events.map(event => event.event), ["connected", "recording", "stopped", "recording", "stopped", "recording", "stopped"]);
        assert.equal(events[2].reason, "target_reached");
        assert.equal(events[2].estimates, 3);
        assert.equal(events[4].reason, "stopped");
        assert.equal(events[4].estimates, 2);
        assert.equal(events[6].reason, "stopped");
        assert.equal(events[6].estimates, 2);
        assert.equal(fs.readFileSync(path.join(folder, "first.jsonl"), "utf8"), expected[0]);
        assert.equal(fs.readFileSync(path.join(folder, "second.jsonl"), "utf8"), expected[1]);
        assert.equal(fs.readFileSync(path.join(folder, "third.jsonl"), "utf8"), expected[2]);
        assert.equal(fs.readdirSync(folder).length, 6);
    } finally {
        input.destroy();
        assert.equal(path.dirname(folder), os.tmpdir());
        fs.rmSync(folder, { recursive: true, force: true });
    }
}

function checkObserver() {
    const events = [];
    const starts = [];
    let states = 0;
    const worker = {
        onStateMessage() { states += 1; },
        async startMeasurement(id, config) { starts.push({ id, config }); return true; },
    };
    const context = vm.createContext({ require, __dirname: path.join(root, "support"),
        process: { argv: [process.execPath, "genie_analyzer.cjs", "profile", "recording"], env: {},
            stdin: { on() {}, resume() {} }, on() {} }, Buffer, setTimeout, clearTimeout, console,
        document: { getElementById: () => ({ __vue_app__: { config: { globalProperties: {
            $pinia: { _s: new Map([["localSession", { worker }]]) },
        } } } }) },
        window: { genieTelemetry: value => events.push(JSON.parse(value)) }, performance: { now: () => 5 },
    });
    vm.runInContext(definitions, context);
    vm.runInContext('observeAnalyzer({name:"accuracy",algoMode:0,chMapPreset:2,scheduling:0})', context);
    worker.onStateMessage({ message: "not inside trial" });
    assert.equal(events.length, 0);
    vm.runInContext('window.genieObserver.active = true', context);
    worker.onStateMessage({ message: "diagnostic" });
    assert.equal(states, 2);
    assert.equal(events[0].kind, "state");
    return worker.startMeasurement("r", { algoMode: 2, antennaPath: 7, connectionInterval: 8 }).then(() => {
        assert.equal(starts[0].config.algoMode, 0);
        assert.equal(starts[0].config.antennaPath, 7);
        assert.equal(starts[0].config.connectionInterval, 8);
        assert.equal(events[1].configuration.start_accepted, true);
        assert.equal(events[1].configuration.profile, "accuracy");
    });
}

async function checkSingleTrial() {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "genie-recorder-test-"));
    const output = path.join(folder, "single.jsonl");
    const input = new PassThrough();
    const processMock = new EventEmitter();
    Object.assign(processMock, { argv: [process.execPath, "genie_analyzer.cjs", folder, output],
        env: {}, stdin: input, execPath: process.execPath });
    const events = [];
    let receive = () => {};
    const debuggerMock = {
        receive(callback) { receive = callback; },
        check() {},
        async evaluate(fn) {
            if (fn.toString().includes("const originalPicker")) receive({ bytes: Buffer.from(line(1)).toString("base64") });
            else receive({ close: true });
            return true;
        },
    };
    const context = vm.createContext({ require, __dirname: path.join(root, "support"), process: processMock, Buffer,
        setTimeout, clearTimeout, console: { error() {}, log(value) {
            events.push(value);
            if (value === "READY") input.end();
        } } });
    context.debuggerMock = debuggerMock;
    try {
        vm.runInContext(definitions, context);
        await vm.runInContext('record(debuggerMock, "record-button")', context);
        assert.deepEqual(events, ["READY", "stopped"]);
        assert.equal(fs.readFileSync(output, "utf8"), line(1));
    } finally {
        input.destroy();
        assert.equal(path.dirname(folder), os.tmpdir());
        fs.rmSync(folder, { recursive: true, force: true });
    }
}

async function checkNoReadings() {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "genie-empty-recording-test-"));
    const output = path.join(folder, "empty.jsonl");
    const input = new PassThrough();
    const events = [];
    let receive = () => {};
    let clock = 0;
    let advanceClock = false;
    const processMock = new EventEmitter();
    Object.assign(processMock, { argv: [process.execPath, "genie_analyzer.cjs", folder, output],
        env: {}, stdin: input, execPath: process.execPath });
    const debuggerMock = {
        receive(callback) { receive = callback; }, check() {},
        async evaluate(fn) {
            if (fn.toString().includes("const originalPicker")) advanceClock = true;
            else { advanceClock = false; receive({ close: true }); }
            return true;
        },
    };
    const context = vm.createContext({ require, __dirname: path.join(root, "support"),
        process: processMock, Buffer, setTimeout, clearTimeout,
        Date: { now: () => advanceClock ? (clock += 31000) : 0 },
        console: { error() {}, log(value) { events.push(value); } }, debuggerMock });
    try {
        vm.runInContext(definitions, context);
        await vm.runInContext('record(debuggerMock, "record-button")', context);
        assert.deepEqual(events, ["READY", "no_readings"]);
        assert.equal(fs.readFileSync(output, "utf8"), "");
        assert(fs.existsSync(output + ".telemetry.jsonl"));
    } finally {
        input.destroy();
        assert.equal(path.dirname(folder), os.tmpdir());
        fs.rmSync(folder, { recursive: true, force: true });
    }
}

async function checkUnusableResultLimit() {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "genie-unusable-recording-test-"));
    const output = path.join(folder, "unusable.jsonl");
    const input = new PassThrough();
    const events = [];
    let receive = () => {};
    const processMock = new EventEmitter();
    Object.assign(processMock, { argv: [process.execPath, "genie_analyzer.cjs", folder, output],
        env: {}, stdin: input, execPath: process.execPath });
    const debuggerMock = {
        receive(callback) { receive = callback; }, check() {},
        async evaluate(fn) {
            if (fn.toString().includes("const originalPicker")) {
                receive({ bytes: Buffer.from(line(null).repeat(1000)).toString("base64") });
            } else receive({ close: true });
            return true;
        },
    };
    const context = vm.createContext({ require, __dirname: path.join(root, "support"),
        process: processMock, Buffer, setTimeout, clearTimeout,
        console: { error() {}, log(value) { events.push(value); } }, debuggerMock });
    try {
        vm.runInContext(definitions, context);
        await vm.runInContext('record(debuggerMock, "record-button")', context);
        assert.deepEqual(events, ["READY", "result_limit"]);
        assert.equal(fs.readFileSync(output, "utf8"), line(null).repeat(1000));
    } finally {
        input.destroy();
        assert.equal(path.dirname(folder), os.tmpdir());
        fs.rmSync(folder, { recursive: true, force: true });
    }
}

checkAntennaModes();
checkSession().then(checkSingleTrial).then(checkNoReadings).then(checkUnusableResultLimit).then(checkObserver).then(() => console.log("Antenna modes, recorder, zero/unusable-reading safeguards and configuration/diagnostic checks passed"))
    .catch(error => { console.error(error); process.exitCode = 1; });
