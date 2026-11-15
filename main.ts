import { createApp } from './app.ts';

// The env owns its port; this process advertises a kernel-assigned port instead.
const origin = (process.env.MAKEFUN_ENV_URL ?? '').replace(/\/+$/, '');
if (!origin) {
  process.stderr.write('[entry] MAKEFUN_ENV_URL must name the env origin\n');
  process.exit(1);
}

const requestTimeoutMs = 5_000;
const firstRetryMs = 250;
const longestRetryMs = 2_000;

function retryTimer(): () => Promise<void> {
  let delay = firstRetryMs;
  return async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, longestRetryMs);
  };
}

async function announcePort(port: number): Promise<void> {
  const pause = retryTimer();
  for (;;) {
    let response: Response;
    try {
      response = await fetch(`${origin}/register`, {
        method: 'POST',
        redirect: 'manual',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ port }),
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
    } catch {
      await pause();
      continue;
    }

    // Registration is decided by the status, not by the response body.
    const status = response.status;
    await response.body?.cancel().catch(() => {});
    if (status === 200 || status === 409) return;
    if (status === 400 || status === 405) {
      throw new Error(`env rejected registration with HTTP ${status}`);
    }
    await pause();
  }
}

async function awaitCompletion(): Promise<void> {
  const pause = retryTimer();
  for (;;) {
    try {
      const response = await fetch(`${origin}/done`, {
        method: 'GET',
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
      if (response.ok) {
        const result: unknown = await response.json();
        if (typeof result === 'object' && result !== null &&
            'done' in result && result.done === true) return;
      } else {
        await response.body?.cancel();
      }
    } catch {
      // A timeout, transport failure, or invalid body is not a completion signal.
    }
    // The env answers immediately, so even successful "not done" polls must wait.
    await pause();
  }
}

async function run(): Promise<void> {
  const server = createApp();
  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error) => reject(error);
    server.once('error', onError);
    server.listen(0, () => {
      server.off('error', onError);
      resolve();
    });
  });

  const address = server.address();
  if (address === null || typeof address === 'string') {
    throw new Error('expected a listening TCP server');
  }
  process.stdout.write(`[entry] serving on port ${address.port}\n`);
  await announcePort(address.port);
  process.stdout.write('[entry] waiting for env completion\n');
  await awaitCompletion();

  await new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });
  process.stdout.write('[entry] env completed; server closed\n');
}

try {
  await run();
} catch (error) {
  process.stderr.write(`[entry] ${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
}
process.exit(0);
