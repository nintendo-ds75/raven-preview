// Whether a long bridge_wait survives a real MCP client: the MCP TypeScript
// SDK, a request timeout shorter than the wait, reset on progress (as Claude
// Code's idle timer is). Setup: npm install @modelcontextprotocol/sdk@1, then
//   BRIDGE_URL=http://127.0.0.1:8765 [BRIDGE_TOKEN=<agent token>] node mcp_progress_probe.mjs
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const base = process.env.BRIDGE_URL || "http://127.0.0.1:8765";
const headers = process.env.BRIDGE_TOKEN ? { Authorization: `Bearer ${process.env.BRIDGE_TOKEN}` } : {};
const seconds = Number(process.env.WAIT_SECONDS || 70);
const client = new Client({ name: "bridge-progress-probe", version: "1.0.0" });
await client.connect(new StreamableHTTPClientTransport(new URL(base + "/mcp"), { requestInit: { headers } }));
const call = async (name, args, opts) => JSON.parse((await client.callTool({ name, arguments: args }, undefined, opts)).content[0].text);
const kick = await call("bridge_start_task", { title: "Change the overage rate", repo: "acme/progress-probe", paths: "billing/rates.py" });
await call("bridge_add_node", { task_id: kick.task_id, question: "Which rate applies above the included quota?", paths: "billing/rates.py" });
const progress = [];
const started = Date.now();
const streamed = await call("bridge_wait", { task_id: kick.task_id, timeout: String(seconds) },
  { onprogress: p => progress.push(p.progress), timeout: 20000, resetTimeoutOnProgress: true });
const took = (Date.now() - started) / 1000;
const plain = await call("bridge_wait", { task_id: kick.task_id, timeout: String(seconds) });
console.log(JSON.stringify({
  with_progress: { seconds: took, progress, timed_out: streamed.timed_out,
                   timeout_applied: streamed.timeout_applied, notice: streamed.notice || "" },
  without_progress: { timeout_applied: plain.timeout_applied, notice: plain.notice || "" },
}, null, 2));
await client.close();
