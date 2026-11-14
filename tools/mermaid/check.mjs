// Reads a JSON array of Mermaid sources on stdin; prints a JSON array with null (parses)
// or the parser's error message for each. Mermaid needs a DOM to load, so jsdom gives it one.
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

let input = "";
for await (const chunk of process.stdin) input += chunk;
const results = [];
for (const source of JSON.parse(input)) {
  try {
    await mermaid.parse(source);
    results.push(null);
  } catch (error) {
    results.push(String(error?.message ?? error).slice(0, 600));
  }
}
process.stdout.write(JSON.stringify(results));
