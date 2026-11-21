const HIDDEN_PROTOCOL_TAG = new RegExp(
  '<state>|<hne-image' + String.fromCharCode(92) + 'b',
  'i',
);
const SCAN_OVERLAP = 12;

function scheduleFrame(callback) {
  if (typeof globalThis.requestAnimationFrame === 'function') {
    return { kind: 'animation', id: globalThis.requestAnimationFrame(callback) };
  }
  return { kind: 'timeout', id: globalThis.setTimeout(callback, 16) };
}

function cancelFrame(frame) {
  if (frame.kind === 'animation' && typeof globalThis.cancelAnimationFrame === 'function') {
    globalThis.cancelAnimationFrame(frame.id);
  } else if (frame.kind === 'timeout') {
    globalThis.clearTimeout(frame.id);
  }
}

/** Accumulate token deltas and publish at most once per rendered frame. */
export class StreamTextBuffer {
  #onChange;
  #visibleChunks = [];
  #visibleLength = 0;
  #scanTail = '';
  #pending = [];
  #frame = null;
  #cutAt = null;

  constructor(onChange) {
    this.#onChange = onChange;
  }

  append(delta) {
    if (!delta || this.#cutAt !== null) return false;
    this.#pending.push(String(delta));
    if (this.#frame) return false;

    this.#frame = scheduleFrame(() => {
      this.#frame = null;
      this.#flushPending();
    });
    return true;
  }

  flush() {
    if (!this.#pending.length) return false;
    if (this.#frame) {
      cancelFrame(this.#frame);
      this.#frame = null;
    }
    return this.#flushPending();
  }

  #flushPending() {
    if (!this.#pending.length) return false;

    const chunk = this.#pending.join('');
    this.#pending.length = 0;
    const start = this.#visibleLength - this.#scanTail.length;
    const match = HIDDEN_PROTOCOL_TAG.exec(this.#scanTail + chunk);

    if (match) {
      this.#cutAt = start + match.index;
      const chunkPrefixLength = Math.max(0, this.#cutAt - this.#visibleLength);
      const visible = (
        this.#visibleChunks.join('').slice(0, this.#cutAt)
        + chunk.slice(0, chunkPrefixLength)
      ).trimEnd();
      this.#visibleChunks = [visible];
      this.#visibleLength = visible.length;
      this.#scanTail = '';
      this.#onChange({ delta: visible, reset: true });
    } else {
      this.#visibleChunks.push(chunk);
      this.#visibleLength += chunk.length;
      this.#scanTail = (this.#scanTail + chunk).slice(-SCAN_OVERLAP);
      this.#onChange({ delta: chunk, reset: false });
    }

    return true;
  }
}
