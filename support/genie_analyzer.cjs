// Drive the installed Analyzer 1.2.2 window and save its per-reflector stream.
// Runs with the Node runtime bundled with Analyzer. The debugger listens locally
// on an ephemeral port for the lifetime of the Analyzer window started here.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const { pathToFileURL } = require("node:url");
const readline = require("node:readline");

const [profile, recording, mode] = process.argv.slice(2);
const sessionMode = mode === "--session";
const noReadingTimeout = 10000;
// Match PBR to the firmware's available logical antenna paths.
const availableAntennas = Math.min(...["bt_cs_ncp", "bt_cs_soc_reflector_studio"].map(project => {
    const header = path.join(__dirname, "..", "firmware", project, "config", "sl_rail_util_cs_antenna_offset_config.h");
    const source = fs.readFileSync(header, "utf8");
    const count = source.match(/^\s*#define\s+SL_RAIL_UTIL_CS_ANTENNA_COUNT\s+(\d+)\s*$/m);
    if (!count) throw new Error(`Cannot read the antenna count from ${header}`);
    return Number(count[1]);
}));
const pbrAntennas = sessionMode ? availableAntennas : Number(process.env.GENIE_PBR_ANTENNAS || availableAntennas);
const initiatorPort = process.env.GENIE_INITIATOR_PORT;
if (![1, 2].includes(pbrAntennas)) throw new Error("GENIE_PBR_ANTENNAS must be 1 or 2");
if (pbrAntennas > availableAntennas) throw new Error("PBR requests more antennas than the firmware configuration provides");
let stopRequested = false;
let captureStopRequested = false;
let sessionBusy = false;
let inputError;
let errorReported = false;
const commands = [];
const emit = event => console.log(JSON.stringify(event));
if (sessionMode) {
    const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    input.on("line", line => {
        try {
            const command = JSON.parse(line);
            if (command.command === "quit") {
                stopRequested = true;
            } else if (command.command === "stop") {
                if (sessionBusy || commands.length) captureStopRequested = true;
            } else if (command.command === "start") {
                if (sessionBusy || commands.length) throw new Error("A recording is already pending or active");
                if (typeof command.path !== "string" || !path.isAbsolute(command.path)) {
                    throw new Error("Recording path must be absolute");
                }
                const maximum = command.max_estimates ?? 100;
                if (!Number.isSafeInteger(maximum) || maximum <= 0) {
                    throw new Error("max_estimates must be a positive integer");
                }
                captureStopRequested = false;
                commands.push({ path: command.path, max_estimates: maximum });
            } else {
                throw new Error("Unknown recording command");
            }
        } catch (error) {
            inputError = error;
            stopRequested = true;
        }
    });
    input.on("close", () => { stopRequested = true; });
} else {
    process.stdin.on("end", () => { stopRequested = true; });
    process.stdin.resume();
}
process.on("SIGINT", () => { stopRequested = true; });

async function waitFor(description, check, timeout, cancelOnStop = true) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
        if (cancelOnStop && stopRequested) throw new Error("Recording canceled");
        const result = await check();
        if (result) return result;
        await delay(150);
    }
    throw new Error(`Timed out waiting for ${description}`);
}

async function openDebugger(analyzer) {
    const rendererPath = path.join(path.dirname(process.execPath), "resources", "app",
        ".webpack", "renderer", "main_window", "index.html");
    const rendererUrl = pathToFileURL(rendererPath).href;
    const endpoint = await waitFor("the Analyzer window", async () => {
        if (analyzer.exitCode !== null) throw new Error("Analyzer exited during startup");
        const portFile = path.join(profile, "DevToolsActivePort");
        if (!fs.existsSync(portFile)) return null;
        const port = Number(fs.readFileSync(portFile, "utf8").split("\n")[0]);
        if (!Number.isInteger(port) || port <= 0 || port > 65535) return null;
        const host = `127.0.0.1:${port}`;
        let pages;
        try {
            const response = await fetch(`http://${host}/json/list`, { signal: AbortSignal.timeout(1000) });
            if (!response.ok) return null;
            pages = await response.json();
        } catch {
            return null;
        }
        const page = pages.find(item => item.type === "page" && item.url.split("#")[0] === rendererUrl);
        return page?.webSocketDebuggerUrl.startsWith(`ws://${host}/devtools/page/`)
            ? page.webSocketDebuggerUrl : null;
    }, 20000);

    const socket = new WebSocket(endpoint);
    const pending = new Map();
    let nextId = 0;
    let onData = () => {};
    let failure;
    socket.addEventListener("message", event => {
        const message = JSON.parse(event.data);
        if (message.method === "Runtime.exceptionThrown") {
            const details = message.params.exceptionDetails;
            console.error(`Analyzer exception: ${details.exception?.description || details.text}`);
        }
        if (message.method === "Runtime.bindingCalled" && message.params.name === "genieRecordChunk") {
            try {
                onData(JSON.parse(message.params.payload));
            } catch (error) {
                failure = error;
            }
            return;
        }
        const reply = pending.get(message.id);
        if (reply) {
            pending.delete(message.id);
            clearTimeout(reply.timer);
            const problem = message.error || message.result?.exceptionDetails;
            if (problem) reply.reject(new Error(problem.exception?.description || problem.message || problem.text));
            else reply.resolve(message.result);
        }
    });
    socket.addEventListener("close", () => {
        failure ??= new Error("Analyzer closed its recording connection");
        for (const reply of pending.values()) {
            clearTimeout(reply.timer);
            reply.reject(failure);
        }
        pending.clear();
    });
    const request = (method, params) => new Promise((resolve, reject) => {
        if (socket.readyState !== WebSocket.OPEN) {
            reject(failure || new Error("Analyzer is unavailable"));
            return;
        }
        const id = ++nextId;
        const timer = setTimeout(() => {
            pending.delete(id);
            reject(new Error(`Analyzer did not respond to ${method}`));
        }, 5000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
    });
    await waitFor("the local Analyzer connection", () => socket.readyState === WebSocket.OPEN, 5000);
    await request("Runtime.enable", {});
    return {
        request,
        async evaluate(fn, ...args) {
            const result = await request("Runtime.evaluate", {
                expression: `(${fn.toString()})(...${JSON.stringify(args)})`,
                returnByValue: true, awaitPromise: true, userGesture: true,
            });
            return result.result.value;
        },
        receive(callback) { onData = callback; },
        check() { if (failure) throw failure; },
        close() { socket.close(); },
    };
}

async function connectBoards(debuggerClient) {
    const initiator = await waitFor("a connected J-Link USB serial port", () => debuggerClient.evaluate(async selectedPort => {
        if (!window.electronSerialBridge) return null;
        const ports = await window.electronSerialBridge.listPorts();
        if (!Array.isArray(ports)) return null;
        const candidates = ports.filter(port => port.vendorId?.toLowerCase() === "1366"
            && (!selectedPort || port.displayName === selectedPort || port.displayName.endsWith(`(${selectedPort})`)));
        if (candidates.length > 1) throw new Error("Multiple J-Link ports are available. Set GENIE_INITIATOR_PORT to the initiator COM port.");
        return candidates[0]?.displayName;
    }, initiatorPort), 12000);
    console.error(`Connecting to ${initiator}`);
    await waitFor("Analyzer to connect the initiator", () => debuggerClient.evaluate(port => {
        const top = document.querySelector('[data-cy="ml-connection-btn"]');
        if (top?.textContent.trim() === "Disconnect") return true;
        const dialog = document.querySelector('[data-cy="Connection-dialog"]');
        if (!dialog) {
            if (top && !top.disabled) top.click();
            return false;
        }
        const field = dialog.querySelector(".serial-option-port-selector");
        const input = field?.querySelector("input");
        const connect = dialog.querySelector('[data-cy="cm-connect-btn"]');
        if (input?.value === port && connect && !connect.disabled) {
            const flowLabel = [...dialog.querySelectorAll("strong")]
                .find(label => label.textContent.trim() === "Flow Control");
            const flowInput = flowLabel?.parentElement.parentElement.querySelector("input");
            if (!flowInput || flowInput.value !== "hardware") {
                if (flowInput?.getAttribute("aria-expanded") === "true") {
                    const menu = document.getElementById(flowInput.getAttribute("aria-controls"));
                    [...(menu?.querySelectorAll('[role="option"]') || [])]
                        .find(option => option.textContent.trim() === "hardware")?.click();
                } else {
                    flowInput?.click();
                }
                return false;
            }
            if (!connect.dataset.genieClicked) {
                connect.dataset.genieClicked = "true";
                connect.click();
            }
        } else if (input?.getAttribute("aria-expanded") === "true") {
            const menu = document.getElementById(input.getAttribute("aria-controls"));
            const option = [...(menu?.querySelectorAll('[role="option"]') || [])]
                .find(item => item.textContent.trim() === port);
            option?.click();
        } else {
            input?.click();
        }
        return false;
    }, initiator), 18000);

    const reflector = await waitFor("a ranging reflector", () => debuggerClient.evaluate(antennas => {
        const dialog = document.querySelector('[data-cy="ml-reflector-connection-dialog"]');
        if (dialog) {
            for (const name of ["initiator", "reflector"]) {
                const field = dialog.querySelector(`[data-cy="refset-${name}-antenna"]`);
                if (!field) return null; // Capabilities load before the antenna controls appear.
                const value = field.textContent.trim();
                if (value === String(antennas)) continue;
                const input = field.querySelector("input");
                if (input?.getAttribute("aria-expanded") === "true") {
                    const menu = document.getElementById(input.getAttribute("aria-controls"));
                    const option = [...(menu?.querySelectorAll('[role="option"]') || [])]
                        .find(item => item.textContent.trim() === String(antennas));
                    option?.click();
                } else {
                    input?.click();
                }
                return null;
            }
            const confirm = [...dialog.querySelectorAll("button")].find(button => button.textContent.trim() === "OK");
            if (confirm && !confirm.disabled && !confirm.dataset.genieClicked) {
                confirm.dataset.genieClicked = "true";
                confirm.click();
            }
            return null;
        }
        const connected = [...document.querySelectorAll('button[data-cy^="ml-reflector-record-btn-"]:enabled')];
        if (connected.length > 1) throw new Error("Multiple reflectors are connected. Use manual live mode to choose one.");
        if (connected.length === 1) return connected[0].querySelector(".mdi-record") && connected[0].dataset.cy;
        const available = [...document.querySelectorAll('button[data-cy^="ml-reflector-connect-btn-"]:enabled')];
        if (available.length > 1) throw new Error("Multiple reflectors were discovered. Use manual live mode to choose one.");
        if (available.length === 1) available[0].click();
        return null;
    }, pbrAntennas), 20000);
    console.error(`PBR antenna counts: ${pbrAntennas} initiator, ${pbrAntennas} reflector`);
    console.error(`Starting recording for ${reflector.slice("ml-reflector-record-btn-".length)}`);
    return reflector;
}

async function record(debuggerClient, control, output = recording, maximum = null) {
    const swap = output + ".crswap";
    if (fs.existsSync(output) || fs.existsSync(swap)) throw new Error(`Recording already exists: ${output}`);
    let file;
    let closed = false;
    let started = false;
    let lastReading = 0;
    let estimates = 0;
    let pending = Buffer.alloc(0);
    let stopReason = "stopped";
    let failure;
    const shouldStop = () => stopRequested || (sessionMode && captureStopRequested);
    try {
        debuggerClient.receive(data => {
            if (data.abort) throw new Error("Analyzer aborted the capture");
            if (data.close) closed = true;
            else {
                const bytes = Buffer.from(data.bytes, "base64");
                if (!bytes.length) return;
                file ??= fs.openSync(swap, "wx");
                for (let offset = 0; offset < bytes.length;) {
                    offset += fs.writeSync(file, bytes, offset, bytes.length - offset);
                }
                // Preserve every byte, then inspect only complete result lines.
                pending = Buffer.concat([pending, bytes]);
                let newline;
                while ((newline = pending.indexOf(10)) !== -1) {
                    const line = pending.subarray(0, newline).toString("utf8").replace(/^\uFEFF/, "").trim();
                    pending = pending.subarray(newline + 1);
                    if (!line) continue;
                    const result = JSON.parse(line);
                    if (!result?.initiatorId || !result?.reflectorId) continue;
                    lastReading = Date.now();
                    if (typeof result.distanceMain === "number" && Number.isFinite(result.distanceMain)) estimates += 1;
                }
            }
        });
        await waitFor("the reflector Record button", () => debuggerClient.evaluate(key => {
            const button = [...document.querySelectorAll('button[data-cy^="ml-reflector-record-btn-"]:enabled')]
                .find(control => control.dataset.cy === key);
            if (button?.querySelector(".mdi-stop")) throw new Error("The reflector is already recording");
            if (!button?.querySelector(".mdi-record")) return false;
            const originalPicker = window.showSaveFilePicker;
            // Supply an output stream for this Record click. Restore the normal
            // picker immediately. Analyzer still produces the original JSONL.
            window.showSaveFilePicker = async () => {
                window.showSaveFilePicker = originalPicker;
                return { createWritable: async () => new WritableStream({
                    write(chunk) {
                        const bytes = typeof chunk === "string" ? new TextEncoder().encode(chunk)
                            : new Uint8Array(chunk.buffer ?? chunk, chunk.byteOffset ?? 0, chunk.byteLength);
                        let encoded = "";
                        for (const byte of bytes) encoded += String.fromCharCode(byte);
                        window.genieRecordChunk(JSON.stringify({ bytes: btoa(encoded) }));
                    },
                    close() { window.genieRecordChunk('{"close":true}'); },
                    abort() { window.genieRecordChunk('{"abort":true}'); },
                }) };
            };
            try { button.click(); } finally { window.showSaveFilePicker = originalPicker; }
            return true;
        }, control), 6000);
        started = true;

        const firstReadingDeadline = Date.now() + 30000;
        while (!lastReading && !shouldStop() && !closed) {
            debuggerClient.check();
            if (Date.now() >= firstReadingDeadline) throw new Error("Timed out waiting for the first recorded reading");
            await delay(150);
        }
        if (lastReading) {
            if (sessionMode) emit({ event: "recording", path: output });
            else console.log("READY");
        }
        while (!shouldStop() && !closed) {
            debuggerClient.check();
            if (maximum !== null && estimates >= maximum) {
                stopReason = "target_reached";
                break;
            }
            if (Date.now() - lastReading >= noReadingTimeout) {
                stopReason = "no_readings";
                console.error("No new readings for 10 seconds. Finishing with the readings already captured.");
                break;
            }
            await delay(150);
        }
        if (closed && !shouldStop()) stopReason = "analyzer_ended";
    } catch (error) {
        failure = error;
    } finally {
        // Keep receiving during Stop so Analyzer can flush queued original bytes.
        if (started && !closed) {
            try {
                await debuggerClient.evaluate(key => {
                    const button = [...document.querySelectorAll('button[data-cy^="ml-reflector-record-btn-"]:enabled')]
                        .find(control => control.dataset.cy === key);
                    if (button?.querySelector(".mdi-stop")) button.click();
                }, control);
                await waitFor("Analyzer to flush the last reading", () => closed, 6000, false);
            } catch (error) {
                failure ??= error;
            }
        }
        debuggerClient.receive(() => {});
        if (file !== undefined) {
            try { fs.fsyncSync(file); } finally { fs.closeSync(file); }
        }
    }
    if (failure) {
        if (!sessionMode && lastReading) console.log(stopReason === "no_readings" ? stopReason : "analyzer_interrupted");
        throw failure;
    }
    if (file === undefined) fs.writeFileSync(swap, "", { flag: "wx" });
    // A finalized file is eligible for Genie's existing byte-for-byte cleanup.
    fs.renameSync(swap, output);
    if (sessionMode) emit({ event: "stopped", path: output, reason: stopReason, estimates });
    else console.log(stopReason);
}

async function recordSession(debuggerClient, control) {
    emit({ event: "connected" });
    while (!stopRequested) {
        debuggerClient.check();
        const command = commands.shift();
        if (!command) {
            await delay(150);
            continue;
        }
        sessionBusy = true;
        try {
            await record(debuggerClient, control, command.path, command.max_estimates);
        } finally {
            sessionBusy = false;
        }
    }
    if (inputError) throw inputError;
}

async function main() {
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const analyzer = spawn(process.execPath, [
        `--user-data-dir=${profile}`, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
    ], { env, stdio: "ignore" });
    let debuggerClient;
    try {
        debuggerClient = await openDebugger(analyzer);
        await waitFor("the Analyzer toolbar", () => debuggerClient.evaluate(() =>
            !!document.querySelector('[data-cy="bglog-btn"]')), 10000);
        const reflector = await connectBoards(debuggerClient);
        await debuggerClient.request("Runtime.addBinding", { name: "genieRecordChunk" });
        if (sessionMode) await recordSession(debuggerClient, reflector);
        else await record(debuggerClient, reflector);
    } catch (error) {
        error = inputError || error;
        if (sessionMode) {
            emit({ event: "error", message: error.message });
            errorReported = true;
        }
        if (debuggerClient) {
            try {
                const details = await debuggerClient.evaluate(() => document.body.innerText.slice(-6000));
                console.error(`Analyzer window status:\n${details}`);
            } catch { /* Preserve the original connection error. */ }
        }
        throw error;
    } finally {
        if (debuggerClient) {
            try {
                await debuggerClient.evaluate(() => {
                    const dialog = document.querySelector('[data-cy="ml-reflector-connection-dialog"]');
                    [...(dialog?.querySelectorAll("button") || [])]
                        .find(button => button.textContent.trim() === "Cancel")?.click();
                    for (const button of document.querySelectorAll('button[data-cy^="ml-reflector-disconnect-btn-"]:enabled')) {
                        button.click();
                    }
                });
                await waitFor("the reflector to disconnect", () => debuggerClient.evaluate(() =>
                    !document.querySelector('button[data-cy^="ml-reflector-disconnect-btn-"]')), 2500, false)
                    .catch(error => console.error(`Analyzer disconnect: ${error.message}`));
                await debuggerClient.evaluate(async () => {
                    if (document.querySelector('[data-cy="ml-connection-btn"]')?.textContent.trim() !== "Disconnect") return;
                    const store = document.getElementById("q-app")?.__vue_app__
                        ?.config.globalProperties.$pinia?._s.get("localSession");
                    const worker = store?.worker?.__v_raw || store?.worker;
                    if (worker) await worker.stopScan();
                }).catch(error => console.error(`Analyzer scanner shutdown: ${error.message}`));
                await debuggerClient.evaluate(() => {
                    const cancel = document.querySelector('[data-cy="cm-disconnect-btn"]');
                    if (cancel && !cancel.disabled) {
                        cancel.click();
                        return;
                    }
                    const button = document.querySelector('[data-cy="ml-connection-btn"]');
                    if (button?.textContent.trim() === "Disconnect" && !button.disabled) button.click();
                });
                await waitFor("the initiator to disconnect", () => debuggerClient.evaluate(() =>
                    document.querySelector('[data-cy="ml-connection-btn"]')?.textContent.trim() === "Connect"), 2500, false);
            } catch (error) {
                console.error(`Analyzer disconnect: ${error.message}`);
            }
        }
        debuggerClient?.close();
        if (analyzer.exitCode === null) analyzer.kill();
        process.stdin.pause();
    }
}

main().catch(error => {
    if (sessionMode && !errorReported) emit({ event: "error", message: error.message });
    console.error(`Analyzer automation: ${error.message}`);
    process.exitCode = 1;
});
