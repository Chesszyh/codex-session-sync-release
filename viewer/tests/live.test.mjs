import { test } from "node:test";
import assert from "node:assert/strict";
import { reduce } from "../src/live.js";

test("stream completion replaces provisional text and prevents duplicate suffixes", () => {
  let state = reduce(null, "item/started", { item: { id: "item", type: "agentMessage", text: "" } });
  state = reduce(state, "item/agentMessage/delta", { itemId: "item", delta: "hello " });
  state = reduce(state, "item/agentMessage/delta", { itemId: "item", delta: "world" });
  assert.equal(state.item.text, "hello world");
  state = reduce(state, "item/completed", { item: { id: "item", type: "agentMessage", text: "hello world" } });
  state = reduce(state, "item/agentMessage/delta", { itemId: "item", delta: "world" });
  assert.equal(state.item.text, "hello world");
  assert.equal(state.complete, true);
});
