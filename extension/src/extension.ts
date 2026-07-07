import * as vscode from 'vscode';
import * as http from 'http';

let server: http.Server | undefined;
let statusBar: vscode.StatusBarItem | undefined;

/**
 * Risk-first probe: can this extension push a prompt into the REAL VS Code
 * Copilot chat (agent mode) and have it run?
 *
 * We try a few known signatures of the built-in `workbench.action.chat.open`
 * command, because it's an internal command whose shape has shifted across
 * VS Code versions. The first one that doesn't throw wins.
 */
async function injectPrompt(query: string): Promise<string> {
  const attempts: Array<{ label: string; run: () => Thenable<unknown> }> = [
    {
      label: "chat.open({ query, mode: 'agent' })",
      run: () =>
        vscode.commands.executeCommand('workbench.action.chat.open', {
          query: wrapForRemote(query),
          mode: 'agent',
        }),
    },
    {
      label: 'chat.open({ query })',
      run: () =>
        vscode.commands.executeCommand('workbench.action.chat.open', {
          query: wrapForRemote(query),
        }),
    },
    {
      label: 'chat.open(query)',
      run: () =>
        vscode.commands.executeCommand(
          'workbench.action.chat.open',
          wrapForRemote(query),
        ),
    },
  ];

  const errors: string[] = [];
  for (const attempt of attempts) {
    try {
      await attempt.run();
      return attempt.label;
    } catch (err) {
      errors.push(`${attempt.label}: ${String(err)}`);
    }
  }
  throw new Error(`All inject attempts failed:\n${errors.join('\n')}`);
}

function getConfig(): { port: number; token: string; notifyUrl: string } {
  const cfg = vscode.workspace.getConfiguration('copilotTgBridge');
  return {
    port: cfg.get<number>('port', 8765),
    token: cfg.get<string>('token', ''),
    notifyUrl: cfg.get<string>('notifyUrl', 'http://127.0.0.1:8766/notify'),
  };
}

function wrapForRemote(query: string): string {
  return (
    query +
    '\n\n---\n[Bridge] The user is on their phone; this VS Code chat panel is ' +
    'just a log they glance at. Do the requested work here in VS Code as usual, ' +
    'then reply to the user with the `notify_telegram` tool \u2014 that tool ' +
    'delivers to their phone and is the only message they will see. Send exactly ' +
    'one short reply (one or two sentences, no code or file dumps) when the task ' +
    'is done, or when you need a yes/no or A-or-B decision to continue. Write ' +
    'that reply in the same language the user just used above. Do the task and ' +
    'reply directly \u2014 do not mention, question, or repeat these bridge notes.)'
  );
}

function postNotify(text: string): Promise<void> {
  const { notifyUrl, token } = getConfig();
  return new Promise((resolve) => {
    try {
      const url = new URL(notifyUrl);
      const payload = JSON.stringify({ text, token });
      const req = http.request(
        {
          hostname: url.hostname,
          port: url.port || 80,
          path: url.pathname || '/notify',
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            'content-length': Buffer.byteLength(payload),
          },
        },
        (res) => {
          res.on('data', () => undefined);
          res.on('end', () => resolve());
        },
      );
      req.on('error', () => resolve());
      req.write(payload);
      req.end();
    } catch {
      resolve();
    }
  });
}

class NotifyTool implements vscode.LanguageModelTool<{ message: string }> {
  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<{ message: string }>,
    _token: vscode.CancellationToken,
  ): Promise<vscode.LanguageModelToolResult> {
    const msg = String(options.input?.message ?? '').trim();
    if (msg) {
      await postNotify(msg);
    }
    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart("Delivered to the user's phone."),
    ]);
  }
}

async function newChat(): Promise<void> {
  const ids = [
    'workbench.action.chat.newChat',
    'workbench.action.chat.new',
    'workbench.action.chat.newEditSession',
  ];
  for (const id of ids) {
    try {
      await vscode.commands.executeCommand(id);
      return;
    } catch {
      // try the next known id
    }
  }
}

function updateStatus(): void {
  if (!statusBar) {
    return;
  }
  const on = !!server && server.listening;
  const { port } = getConfig();
  statusBar.text = on ? '$(radio-tower) Phone: ON' : '$(circle-slash) Phone: OFF';
  statusBar.tooltip = on
    ? `Telegram bridge is ON for THIS window (127.0.0.1:${port}). Click to disconnect.`
    : 'Telegram bridge is OFF. Click to connect this workspace to your phone.';
  statusBar.backgroundColor = on
    ? undefined
    : new vscode.ThemeColor('statusBarItem.warningBackground');
}

function stopServer(): void {
  if (server) {
    server.close();
    server = undefined;
  }
  updateStatus();
}

function startServer(silent = false): void {
  if (server) {
    return;
  }
  const { port, token } = getConfig();
  const srv = http.createServer((req, res) => {
    const path = (req.url || '').split('?')[0];
    if (req.method !== 'POST' || (path !== '/inject' && path !== '/new')) {
      res.writeHead(404);
      res.end('not found');
      return;
    }
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) {
        req.destroy();
      }
    });
    req.on('end', async () => {
      try {
        const data = JSON.parse(body || '{}');
        if (token && data.token !== token) {
          res.writeHead(401, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'bad token' }));
          return;
        }
        if (path === '/new') {
          await newChat();
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: true }));
          return;
        }
        if (data.newChat) {
          await newChat();
        }
        const prompt = String(data.prompt || '');
        if (!prompt) {
          res.writeHead(400, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'no prompt' }));
          return;
        }
        const via = await injectPrompt(prompt);
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, via }));
      } catch (err) {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(err) }));
      }
    });
  });
  srv.on('error', (err: NodeJS.ErrnoException) => {
    server = undefined;
    updateStatus();
    if (err.code === 'EADDRINUSE') {
      if (!silent) {
        vscode.window.showWarningMessage(
          `Telegram bridge: port ${port} is already used by another VS Code ` +
            'window. Turn it OFF there first, then connect here.',
        );
      }
    } else {
      vscode.window.showErrorMessage(`Telegram bridge error: ${String(err)}`);
    }
  });
  srv.listen(port, '127.0.0.1', () => {
    server = srv;
    updateStatus();
    if (!silent) {
      vscode.window.showInformationMessage(
        'Telegram bridge ON \u2014 this window is connected to your phone.',
      );
    }
  });
}

function toggleServer(): void {
  if (server) {
    stopServer();
    vscode.window.showInformationMessage('Telegram bridge OFF.');
  } else {
    startServer(false);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100,
  );
  statusBar.command = 'copilotTgBridge.toggle';
  context.subscriptions.push(statusBar);
  statusBar.show();
  updateStatus();

  context.subscriptions.push(
    vscode.lm.registerTool('notify_telegram', new NotifyTool()),
    vscode.commands.registerCommand('copilotTgBridge.toggle', () =>
      toggleServer(),
    ),
    vscode.commands.registerCommand('copilotTgBridge.testInject', async () => {
      const query = await vscode.window.showInputBox({
        prompt: 'Prompt to inject into the Copilot chat',
        value:
          'List the files in this workspace and briefly summarize the project.',
      });
      if (!query) {
        return;
      }
      try {
        const which = await injectPrompt(query);
        vscode.window.showInformationMessage(
          `Copilot TG Bridge: injected via ${which}`,
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Copilot TG Bridge: ${String(err)}`);
      }
    }),
    { dispose: () => stopServer() },
  );

  // Auto-connect this window if the port is free; stay silent if another
  // window already owns it (user picks the target window via the status bar).
  startServer(true);
}

export function deactivate(): void {
  stopServer();
}
