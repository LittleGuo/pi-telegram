/**
 * pi-telegram-tui-bridge
 *
 * Independent of pi-super-vim. A small global extension that:
 *   1. Watches %TEMP%/pi-telegram-tui-bridge/<session-id>.req
 *   2. Delivers the text with pi.sendUserMessage() (appears in the TUI)
 *   3. Writes <session-id>.settled when agent_settled fires
 *
 * Only active in TUI mode, so a headless `pi -p` session will not steal requests.
 * Reload with /reload after installing; do not need to remove pi-super-vim.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const CTRL_DIR = path.resolve(
	process.env.PI_TELEGRAM_TUI_BRIDGE_DIR?.trim() || path.join(os.tmpdir(), "pi-telegram-tui-bridge"),
);

type RequestMode = "auto" | "steer" | "followUp";

interface BridgeRequest {
	sid?: string;
	text?: string;
	mode?: RequestMode;
	ts?: number;
}

interface SessionState {
	id: string;
	reqFile: string;
	watcher?: fs.FSWatcher;
	poll?: NodeJS.Timeout;
}

let session: SessionState | undefined;
let latestCtx: ExtensionContext | undefined;
let piApi: ExtensionAPI | undefined;
let delivering = false;

function isTui(ctx: ExtensionContext | undefined): boolean {
	return ctx?.mode === "tui";
}

function mkdirp(dir: string): void {
	fs.mkdirSync(dir, { recursive: true });
}

function atomicWriteJson(file: string, value: unknown): void {
	mkdirp(path.dirname(file));
	const tmp = `${file}.tmp`;
	fs.writeFileSync(tmp, `${JSON.stringify(value)}\n`, "utf8");
	fs.renameSync(tmp, file);
}

function sessionMeta(ctx: ExtensionContext): Record<string, unknown> {
	const sid = ctx.sessionManager.getSessionId() ?? "";
	return {
		sid,
		cwd: ctx.cwd,
		sessionFile: ctx.sessionManager.getSessionFile() ?? null,
		mode: ctx.mode,
		name: ctx.sessionManager.getSessionName?.()?.trim() || null,
		updatedAt: Date.now(),
	};
}

function writePresence(ctx: ExtensionContext): void {
	const sid = ctx.sessionManager.getSessionId();
	if (!sid || !isTui(ctx)) return;
	mkdirp(CTRL_DIR);
	const meta = sessionMeta(ctx);
	atomicWriteJson(path.join(CTRL_DIR, "current.json"), meta);
	atomicWriteJson(path.join(CTRL_DIR, `${sid}.alive`), meta);
}

function writeSettled(ctx: ExtensionContext, aborted: boolean): void {
	const sid = ctx.sessionManager.getSessionId();
	if (!sid || !isTui(ctx)) return;
	writePresence(ctx);
	atomicWriteJson(path.join(CTRL_DIR, `${sid}.settled`), {
		...sessionMeta(ctx),
		event: "agent_settled",
		aborted,
		ts: Date.now(),
	});
}

function clearPresence(ctx: ExtensionContext): void {
	const sid = ctx.sessionManager.getSessionId();
	if (!sid) return;
	try {
		fs.rmSync(path.join(CTRL_DIR, `${sid}.alive`), { force: true });
	} catch {
		// ignore
	}
	try {
		const currentFile = path.join(CTRL_DIR, "current.json");
		if (fs.existsSync(currentFile)) {
			const raw = JSON.parse(fs.readFileSync(currentFile, "utf8")) as { sid?: string };
			if (raw.sid === sid) fs.rmSync(currentFile, { force: true });
		}
	} catch {
		// ignore
	}
}

async function deliver(request: BridgeRequest): Promise<void> {
	const ctx = latestCtx;
	const pi = piApi;
	if (!ctx || !pi || !isTui(ctx)) return;

	const text = typeof request.text === "string" ? request.text.replace(/\r\n/g, "\n").trimEnd() : "";
	if (!text.trim()) return;

	const mode: RequestMode = request.mode === "steer" || request.mode === "followUp" ? request.mode : "auto";
	const busy = !ctx.isIdle();
	const deliverAs = mode === "auto" ? (busy ? "followUp" : undefined) : mode;

	try {
		await pi.sendUserMessage(text, deliverAs ? { deliverAs } : undefined);
	} catch (error) {
		try {
			await pi.sendUserMessage(text, { deliverAs: "followUp" });
		} catch (retryError) {
			try {
				ctx.ui.notify(`telegram bridge failed: ${String(retryError ?? error)}`, "error");
			} catch {
				// UI may be gone
			}
		}
	}
}

async function drainRequests(): Promise<void> {
	const current = session;
	if (!current || delivering) return;
	delivering = true;
	try {
		for (;;) {
			if (session !== current) return;
			let raw: string;
			try {
				raw = fs.readFileSync(current.reqFile, "utf8");
			} catch {
				return;
			}
			try {
				fs.unlinkSync(current.reqFile);
			} catch {
				// someone else already took it
			}
			let request: BridgeRequest;
			try {
				request = JSON.parse(raw) as BridgeRequest;
			} catch {
				continue;
			}
			if (request.sid && request.sid !== current.id) continue;
			await deliver(request);
		}
	} finally {
		delivering = false;
	}
}

function scheduleDrain(): void {
	void drainRequests();
}

function startWatching(id: string): void {
	const reqFile = path.join(CTRL_DIR, `${id}.req`);
	const state: SessionState = { id, reqFile };
	session = state;
	mkdirp(CTRL_DIR);
	fs.rmSync(reqFile, { force: true });
	fs.rmSync(`${reqFile}.tmp`, { force: true });

	try {
		state.watcher = fs.watch(CTRL_DIR, (_event, filename) => {
			if (!filename || String(filename).startsWith(`${id}.req`)) scheduleDrain();
		});
		state.watcher.on("error", () => {
			// polling below is the fallback
		});
	} catch {
		// fs.watch can fail; poll still works
	}
	state.poll = setInterval(scheduleDrain, 1_000);
	state.poll.unref?.();
}

function stopWatching(): void {
	if (!session) return;
	session.watcher?.close();
	if (session.poll) clearInterval(session.poll);
	session = undefined;
}

export default function piTelegramTuiBridge(pi: ExtensionAPI) {
	piApi = pi;

	pi.on("session_start", async (_event, ctx) => {
		latestCtx = ctx;
		stopWatching();
		if (!isTui(ctx)) return;
		const id = ctx.sessionManager.getSessionId();
		if (!id) return;
		startWatching(id);
		writePresence(ctx);
	});

	pi.on("agent_settled", async (event, ctx) => {
		latestCtx = ctx;
		if (!isTui(ctx)) return;
		writeSettled(ctx, event.aborted === true);
	});

	pi.on("session_info_changed", async (_event, ctx) => {
		latestCtx = ctx;
		if (!isTui(ctx)) return;
		writePresence(ctx);
	});

	pi.on("session_shutdown", async (event, ctx) => {
		stopWatching();
		if (event.reason === "reload") return;
		clearPresence(ctx);
	});
}
