'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const source = fs.readFileSync(
  path.resolve(__dirname, '../../web/js/stream-buffer.js'),
  'utf8',
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const bufferModule = import(moduleUrl);

async function withFrameClock(run) {
  const previousSchedule = globalThis.requestAnimationFrame;
  const previousCancel = globalThis.cancelAnimationFrame;
  const frames = new Map();
  let nextId = 0;

  globalThis.requestAnimationFrame = (callback) => {
    const id = ++nextId;
    frames.set(id, callback);
    return id;
  };
  globalThis.cancelAnimationFrame = (id) => frames.delete(id);

  const nextFrame = () => {
    const callbacks = [...frames.values()];
    frames.clear();
    for (const callback of callbacks) callback();
  };

  try {
    await run(frames, nextFrame);
  } finally {
    if (previousSchedule === undefined) delete globalThis.requestAnimationFrame;
    else globalThis.requestAnimationFrame = previousSchedule;
    if (previousCancel === undefined) delete globalThis.cancelAnimationFrame;
    else globalThis.cancelAnimationFrame = previousCancel;
  }
}

test('stream text writes once per frame for thousands of token deltas', async () => {
  const { StreamTextBuffer } = await bufferModule;
  await withFrameClock(async (frames, nextFrame) => {
    const updates = [];
    let visible = '';
    const buffer = new StreamTextBuffer((update) => {
      updates.push(update);
      visible = update.reset ? update.delta : visible + update.delta;
    });
    const parts = Array.from({ length: 4_000 }, (_, index) => `字${index},`);

    for (const part of parts) buffer.append(part);

    assert.equal(frames.size, 1, 'all token deltas should share one scheduled frame');
    assert.equal(updates.length, 0, 'token arrivals must not write to the DOM');
    nextFrame();

    assert.equal(updates.length, 1);
    assert.deepEqual(updates[0], { delta: parts.join(''), reset: false });
    assert.equal(visible, parts.join(''));
  });
});

test('state tags split across frames stay hidden, including all following chunks', async () => {
  const { StreamTextBuffer } = await bufferModule;
  await withFrameClock(async (frames, nextFrame) => {
    const updates = [];
    let visible = '';
    const buffer = new StreamTextBuffer((update) => {
      updates.push(update);
      visible = update.reset ? update.delta : visible + update.delta;
    });

    buffer.append('可见剧情  ');
    nextFrame();
    buffer.append('<sta');
    nextFrame();
    buffer.append('te>隐藏状态');
    nextFrame();
    assert.equal(visible, '可见剧情');
    assert.equal(buffer.append('后续不应出现的状态内容'), false);
    assert.equal(frames.size, 0);
    assert.equal(visible, '可见剧情');
  });
});

test('image protocol tags stay hidden across a token boundary', async () => {
  const { StreamTextBuffer } = await bufferModule;
  await withFrameClock(async (_frames, nextFrame) => {
    const updates = [];
    let visible = '';
    const buffer = new StreamTextBuffer((update) => {
      updates.push(update);
      visible = update.reset ? update.delta : visible + update.delta;
    });

    buffer.append('角色说完了。<HNE-IM');
    nextFrame();
    buffer.append('AGE prompt=hidden');
    nextFrame();

    assert.equal(visible, '角色说完了。');
  });
});

test('flush publishes a pending final delta and cancels its scheduled frame', async () => {
  const { StreamTextBuffer } = await bufferModule;
  await withFrameClock(async (frames) => {
    const updates = [];
    const buffer = new StreamTextBuffer((update) => updates.push(update));

    buffer.append('最后一个字');
    assert.equal(frames.size, 1);
    assert.equal(buffer.flush(), true);
    assert.equal(frames.size, 0);
    assert.deepEqual(updates, [{ delta: '最后一个字', reset: false }]);
    assert.equal(buffer.flush(), false);
  });
});
