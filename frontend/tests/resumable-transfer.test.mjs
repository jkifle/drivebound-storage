import assert from "node:assert/strict";
import test from "node:test";
import { finishTransfer } from "../app/resumable-transfer.ts";

const file = new Blob(["test"]);
const initial = { upload_url: "/uploads/test", offset: 0, chunk_size: 4, status: "active" };
const state = (offset, status = "active") => ({ ...initial, offset, status });
const head = (offset, status = "active") => new Response(null, { status: 204, headers: { "Upload-Offset": String(offset), "Upload-Status": status } });
const noWait = async () => {};

test("failed publication after all bytes arrived sends an empty finalization retry", async () => {
  const bodies = [];
  let patches = 0;
  const result = await finishTransfer(file, initial, async (_, init) => {
    if (init.method === "HEAD") return head(4);
    bodies.push(init.body.size);
    return ++patches === 1 ? new Response("failed", { status: 500 }) : Response.json(state(4, "complete"));
  }, () => {}, noWait);
  assert.equal(result.status, "complete");
  assert.deepEqual(bodies, [4, 0]);
});

test("permanent drive error is not reported as complete", async () => {
  let patches = 0;
  await assert.rejects(finishTransfer(file, initial, async (_, init) => {
    if (init.method === "HEAD") return head(4);
    patches++;
    return Response.json({ detail: "Destination drive is full" }, { status: 503 });
  }, () => {}, noWait), /drive is full/);
  assert.equal(patches, 4);
});

test("lost successful final response is recovered from confirmed HEAD status", async () => {
  const result = await finishTransfer(file, initial, async (_, init) => {
    if (init.method === "HEAD") return head(4, "complete");
    throw new Error("lost response");
  }, () => {}, noWait);
  assert.equal(result.status, "complete");
});

test("manual retry at full offset sends no file bytes", async () => {
  await finishTransfer(file, state(4), async (_, init) => {
    assert.equal(init.body.size, 0);
    assert.equal(init.headers["Upload-Offset"], "4");
    return Response.json(state(4, "complete"));
  }, () => {}, noWait);
});

test("partial lost response resumes only the missing bytes", async () => {
  const sizes = [];
  await finishTransfer(file, initial, async (_, init) => {
    if (init.method === "HEAD") return head(2);
    sizes.push(init.body.size);
    if (sizes.length === 1) throw new Error("lost");
    return Response.json(state(4, "complete"));
  }, () => {}, noWait);
  assert.deepEqual(sizes, [4, 2]);
});

test("offline server and a server making no progress have bounded retries", async () => {
  for (const offline of [true, false]) {
    let requests = 0;
    await assert.rejects(finishTransfer(file, initial, async () => {
      requests++;
      if (offline) throw new Error("offline");
      return Response.json(initial);
    }, () => {}, noWait), /did not finish/);
    assert.equal(requests, offline ? 6 : 3);
  }
});

test("invalid offsets and premature completion are rejected", async () => {
  for (const returned of [state(-1), state(9), state(1, "complete")]) {
    await assert.rejects(finishTransfer(file, initial, async () => Response.json(returned), () => {}, noWait));
  }
});
