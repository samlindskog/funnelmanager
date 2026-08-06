/** NDJSON stream reader for the agents sessions turn stream.
 *
 * Reads a `Response` body incrementally via a ReadableStream reader with line
 * buffering, yielding one parsed JSON object per `\n`-delimited line as it
 * arrives — so the transcript updates LIVE, not after the whole turn completes.
 * A line that fails to parse is skipped (never throws mid-stream). The caller
 * treats an in-stream `{type:"error"}` line as a transcript error, never a fatal
 * throw (P8). */
export async function* readNdjson(response: Response): AsyncGenerator<unknown> {
  const body = response.body
  if (!body) return
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (line) {
          const parsed = tryParse(line)
          if (parsed !== undefined) yield parsed
        }
      }
    }
    const tail = buffer.trim()
    if (tail) {
      const parsed = tryParse(tail)
      if (parsed !== undefined) yield parsed
    }
  } finally {
    reader.releaseLock()
  }
}

function tryParse(line: string): unknown {
  try {
    return JSON.parse(line)
  } catch {
    return undefined
  }
}
