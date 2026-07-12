import { mkdir, readFile, writeFile, copyFile } from "node:fs/promises";

const html = await readFile("index.html", "utf8");

await mkdir("dist/server", { recursive: true });
await mkdir("dist/privacy", { recursive: true });
await mkdir("dist/.openai", { recursive: true });
await copyFile("index.html", "dist/index.html");
await copyFile("index.html", "dist/privacy/index.html");
await copyFile(".openai/hosting.json", "dist/.openai/hosting.json");

const server = `const html = ${JSON.stringify(html)};

export default {
  async fetch() {
    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300"
      }
    });
  }
};
`;

await writeFile("dist/server/index.js", server);
