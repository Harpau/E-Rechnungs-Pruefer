import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const FLOW_PATH = fileURLToPath(
  new URL("../docs/examples/node-red-e-rechnungs-pruefer-flow.json", import.meta.url),
);
const nodes = JSON.parse(readFileSync(FLOW_PATH, "utf8"));
const byName = new Map(nodes.filter((node) => node.name).map((node) => [node.name, node]));

const REPORT_ARTIFACT = {
  body: Buffer.from("%PDF-1.7\nsynthetic report\n%%EOF\n", "ascii"),
  contentDisposition: "attachment",
  contentType: "application/pdf",
  extension: ".pdf",
};

function functionNode(name) {
  const node = byName.get(name);
  assert.ok(node, `Node fehlt: ${name}`);
  assert.equal(node.type, "function", `${name} ist kein Function-Node`);
  return node;
}

function functionScript(node) {
  return new vm.Script(
    `(function(msg, env, node, Buffer) {\n${node.func}\n})(msg, env, node, Buffer);`,
  );
}

function runFunction(name, msg, environment = {}) {
  const statuses = [];
  const env = {
    get(key) {
      return Object.hasOwn(environment, key) ? environment[key] : undefined;
    },
  };
  const node = {
    status(value) {
      statuses.push(value);
    },
  };
  const context = vm.createContext({ console, Buffer, Date, env, msg, node });
  return { output: functionScript(functionNode(name)).runInContext(context), statuses };
}

function imapContext(uid = 42) {
  return {
    accountId: "imap-account",
    mailbox: "INBOX",
    uid,
    uidValidity: "uid-validity-1",
    ackToken: {
      version: 2,
      accountId: "imap-account",
      host: "imap.example.invalid",
      port: 993,
      secure: true,
      user: "rechnung@example.invalid",
      mailbox: "INBOX",
      uid,
      uidValidity: "uid-validity-1",
      queueKey: "synthetic-queue-key",
      issuedAt: Date.now(),
      nonce: `nonce-${uid}`,
      signature: `synthetic-signature-${uid}`,
    },
    delivery: { mode: "at-least-once", duplicatePossible: true },
  };
}

function readMessageProperty(msg, path) {
  return String(path || "")
    .replace(/^msg\./, "")
    .split(".")
    .filter(Boolean)
    .reduce((value, key) => value?.[key], msg);
}

function executeSmtpMappings(msg) {
  const smtp = byName.get("Prüfbericht versenden");
  const account = nodes.find((item) => item.id === smtp.account);
  const options = {};
  for (const field of [
    "to",
    "cc",
    "bcc",
    "from",
    "replyTo",
    "subject",
    "text",
    "html",
    "attachments",
  ]) {
    const source = smtp[`${field}Source`];
    const configured = smtp[`${field}Value`];
    const value = source === "msg" ? readMessageProperty(msg, configured) : configured;
    if (value !== undefined && value !== null && value !== "") options[field] = value;
  }
  if (!options.from && account.from) options.from = account.from;
  assert.ok(options.to, "SMTP-Empfänger fehlt");
  assert.ok(options.text || options.html, "SMTP-Nachrichteninhalt fehlt");
  return options;
}

function assertIsoTimestamp(value) {
  assert.equal(typeof value, "string");
  assert.equal(new Date(value).toISOString(), value);
}

function candidate(filename = "rechnung.xml", kind = "xml") {
  return {
    filename,
    contentType: kind === "xml" ? "application/xml" : "application/pdf",
    kind,
    content: Buffer.from(kind === "xml" ? "<Invoice/>" : "%PDF-1.7\n", "utf8"),
    originalIndex: 0,
  };
}

function automationState({
  candidates = [candidate()],
  correlationId = "test-correlation",
  requireKosit = true,
} = {}) {
  return {
    candidates,
    candidateIndex: 0,
    retryCount: 0,
    reports: [],
    results: [],
    correlationId,
    requireKosit,
  };
}

function apiResponse({
  syntax = "CII",
  validation = "ok",
  official = "accepted",
  statusCode = 200,
  contentType = REPORT_ARTIFACT.contentType,
  body = REPORT_ARTIFACT.body,
  extraHeaders = {},
  state = automationState(),
} = {}) {
  return {
    _msgid: "response-1",
    automation: state,
    payload: Buffer.from(body),
    statusCode,
    requestTimeout: 90000,
    followRedirects: false,
    headers: {
      "content-type": contentType,
      "x-einvoice-syntax": syntax,
      "x-einvoice-validation-status": validation,
      "x-einvoice-official-status": official,
      ...extraHeaders,
    },
  };
}

test("all Function nodes compile in a real Node.js runtime", () => {
  for (const node of nodes.filter((item) => item.type === "function")) {
    assert.doesNotThrow(
      () => functionScript(node),
      `Function-Node kann nicht kompiliert werden: ${node.name}`,
    );
  }
});

test("candidate preparation filters, sorts and deduplicates attachments", () => {
  const xml = Buffer.from("\ufeff  <Invoice/>", "utf8");
  const pdf = Buffer.from("%PDF-1.7\nsynthetic", "utf8");
  const imap = imapContext(42);
  const msg = {
    _msgid: "mail-42",
    imap,
    payload: "must be removed",
    headers: { stale: "header" },
    statusCode: 999,
    email: {
      attachments: [
        { filename: "zuerst.pdf", contentType: "application/pdf", content: pdf },
        { filename: "rechnung.xml", contentType: "application/xml", content: xml },
        {
          filename: "duplikat.xml",
          contentType: "application/octet-stream",
          content: xml.toString("base64"),
        },
        { filename: "hinweis.txt", contentType: "text/plain", content: Buffer.from("text") },
        { filename: "defekt.xml", contentType: "application/xml", content: "nicht-base64!" },
      ],
    },
  };

  const { output } = runFunction("Alle XML/PDF-Kandidaten vorbereiten", msg, {
    EINVOICE_REQUIRE_KOSIT: "false",
  });

  assert.equal(output[0], msg);
  assert.equal(output[1], null);
  assert.deepEqual(
    Array.from(msg.automation.candidates, (item) => [item.filename, item.kind]),
    [
      ["rechnung.xml", "xml"],
      ["zuerst.pdf", "pdf"],
    ],
  );
  assert.equal(msg.automation.results.length, 1);
  assert.equal(msg.automation.results[0].filename, "defekt.xml");
  assert.equal(msg.automation.requireKosit, false);
  assert.strictEqual(msg.imap, imap);
  assert.strictEqual(msg.imap.ackToken, imap.ackToken);
  assert.equal("payload" in msg, false);
  assert.equal("headers" in msg, false);
  assert.equal("statusCode" in msg, false);
});

test("multipart request is byte-exact, local, authenticated and actually gets a timeout", () => {
  const fileBytes = Buffer.from([0x00, 0x01, 0x02, 0x0d, 0x0a, 0xff]);
  const state = automationState({
    candidates: [
      {
        filename: "Rechnung ä\r\n\".xml",
        contentType: "application/xml",
        kind: "xml",
        content: fileBytes,
        originalIndex: 0,
      },
    ],
  });
  state.retryCount = 2;
  const imap = imapContext(123);
  const msg = { _msgid: "abc-123", automation: state, imap };
  const token = "a".repeat(32);

  const { output } = runFunction("Multipart-API-Aufruf vorbereiten", msg, {
    EINVOICE_API_TOKEN: token,
    EINVOICE_API_URL: "http://localhost:8181/api/report/pdf",
  });

  assert.equal(output[0], msg);
  assert.equal(output[1], null);
  assert.equal(msg.method, "POST");
  assert.equal(msg.url, "http://localhost:8181/api/report/pdf");
  assert.equal(msg.requestTimeout, 90000);
  assert.equal(msg.followRedirects, false);
  assert.equal(msg.headers.Authorization, `Bearer ${token}`);
  assert.equal(msg.headers.Accept, "application/pdf");
  assert.strictEqual(msg.imap, imap);
  assert.strictEqual(msg.imap.ackToken, imap.ackToken);

  const boundary = "----------------einvoice-abc123-0-2";
  assert.equal(msg.headers["Content-Type"], `multipart/form-data; boundary=${boundary}`);
  const expected = Buffer.concat([
    Buffer.from(
      `--${boundary}\r\n` +
        'Content-Disposition: form-data; name="file"; filename="Rechnung_.xml"\r\n' +
        "Content-Type: application/xml\r\n\r\n",
      "utf8",
    ),
    fileBytes,
    Buffer.from(
      `\r\n--${boundary}\r\n` +
        'Content-Disposition: form-data; name="official"\r\n\r\n' +
        `true\r\n--${boundary}--\r\n`,
      "utf8",
    ),
  ]);
  assert.deepEqual(msg.payload, expected);
});

test("EINVOICE_REQUIRE_KOSIT controls the official multipart field end to end", () => {
  const cases = [
    { configured: undefined, expected: true },
    { configured: "true", expected: true },
    { configured: "false", expected: false },
  ];

  for (const { configured, expected } of cases) {
    const msg = {
      _msgid: `mail-kosit-${configured ?? "default"}`,
      email: {
        attachments: [
          {
            filename: "rechnung.xml",
            contentType: "application/xml",
            content: Buffer.from("<Invoice/>", "utf8"),
          },
        ],
      },
    };
    const environment =
      configured === undefined ? {} : { EINVOICE_REQUIRE_KOSIT: configured };

    const { output: preparationOutput } = runFunction(
      "Alle XML/PDF-Kandidaten vorbereiten",
      msg,
      environment,
    );
    assert.equal(preparationOutput[0], msg);
    assert.equal(msg.automation.requireKosit, expected);

    const { output: requestOutput } = runFunction("Multipart-API-Aufruf vorbereiten", msg, {
      EINVOICE_API_TOKEN: "a".repeat(32),
    });
    assert.equal(requestOutput[0], msg);
    assert.ok(
      msg.payload
        .toString("utf8")
        .includes(
          `Content-Disposition: form-data; name="official"\r\n\r\n${expected}\r\n`,
        ),
    );
  }
});

test("multipart configuration errors cannot call a remote endpoint", () => {
  const msg = {
    automation: automationState(),
    payload: Buffer.from("stale"),
    headers: { stale: true },
    requestTimeout: 90000,
    followRedirects: true,
  };
  const { output } = runFunction("Multipart-API-Aufruf vorbereiten", msg, {
    EINVOICE_API_TOKEN: "a".repeat(32),
    EINVOICE_API_URL: "https://example.invalid/api/report/pdf",
  });

  assert.equal(output[0], null);
  assert.equal(output[1], msg);
  assert.equal(msg.automationError.class, "configuration");
  assertIsoTimestamp(msg.automationError.occurredAt);
  assert.equal("payload" in msg, false);
  assert.equal("headers" in msg, false);
  assert.equal("requestTimeout" in msg, false);
  assert.equal("followRedirects" in msg, false);
});

test("local API URLs reject invalid ports, query strings and fragments", () => {
  for (const apiUrl of [
    "http://127.0.0.1:0/api/report/pdf",
    "http://127.0.0.1:99999/api/report/pdf",
    "http://127.0.0.1:8080/api/report/pdf?redirect=1",
    "http://localhost:8080/api/report/pdf#fragment",
  ]) {
    const msg = { automation: automationState() };
    const { output } = runFunction("Multipart-API-Aufruf vorbereiten", msg, {
      EINVOICE_API_TOKEN: "a".repeat(32),
      EINVOICE_API_URL: apiUrl,
    });

    assert.equal(output[0], null);
    assert.equal(output[1], msg);
    assert.equal(msg.automationError.class, "configuration");
    assert.match(msg.automationError.message, /gültigen Port/);
  }
});

test("multipart API tokens must follow the shared URL-safe token contract", () => {
  for (const token of [
    "a".repeat(31),
    `${"a".repeat(31)}+`,
    `${"a".repeat(31)}ä`,
    ` ${"a".repeat(32)}`,
    `${"a".repeat(32)} `,
  ]) {
    const msg = { automation: automationState() };
    const { output } = runFunction("Multipart-API-Aufruf vorbereiten", msg, {
      EINVOICE_API_TOKEN: token,
    });

    assert.equal(output[0], null);
    assert.equal(output[1], msg);
    assert.equal(msg.automationError.class, "configuration");
    assert.match(msg.automationError.message, /URL-sicheren Format/);
    assertIsoTimestamp(msg.automationError.occurredAt);
  }
});

test("status classifier accepts CII and UBL reports and rejects no official invoice result", () => {
  const cases = [
    { syntax: "CII", validation: "ok", official: "accepted" },
    { syntax: "UBL", validation: "warning", official: "rejected" },
  ];

  for (const values of cases) {
    const msg = apiResponse(values);
    const { output } = runFunction("HTTP- und Prüfstatus klassifizieren", msg);

    assert.equal(output[0], msg);
    assert.equal(output[1], null);
    assert.equal(output[2], null);
    assert.equal(msg.automation.results[0].outcome, "processed");
    assert.equal(msg.automation.reports.length, 1);
    assert.ok(msg.automation.reports[0].filename.endsWith(REPORT_ARTIFACT.extension));
    assert.equal(msg.automation.reports[0].contentType, REPORT_ARTIFACT.contentType);
    assert.equal(
      msg.automation.reports[0].contentDisposition,
      REPORT_ARTIFACT.contentDisposition,
    );
    assert.deepEqual(msg.automation.reports[0].content, REPORT_ARTIFACT.body);
    assert.equal(msg.automation.reports[0].official, values.official);
    assert.equal("requestTimeout" in msg, false);
    assert.equal("followRedirects" in msg, false);
  }
});

test("successful responses require both PDF media type and PDF signature", () => {
  const invalidResponses = [
    apiResponse({ body: Buffer.from("<html>not a PDF</html>", "utf8") }),
    apiResponse({ contentType: "text/html", body: REPORT_ARTIFACT.body }),
  ];

  for (const msg of invalidResponses) {
    const { output } = runFunction("HTTP- und Prüfstatus klassifizieren", msg);
    assert.equal(output[0], null);
    assert.equal(output[1], null);
    assert.equal(output[2], msg);
    assert.equal(msg.automationError.class, "protocol");
    assert.match(msg.automationError.message, /PDF- und Statusvertrag/);
    assertIsoTimestamp(msg.automationError.occurredAt);
    assert.equal(msg.automation.reports.length, 0);
  }
});

test("report filenames are unique per message, ASCII-safe and contain no invoice filename", () => {
  function classifyMail(messageId) {
    const sourceFilename = "Original-Rechnung-4711.xml";
    const msg = {
      _msgid: messageId,
      email: {
        attachments: [
          {
            filename: sourceFilename,
            content: Buffer.from("<Invoice/>", "utf8"),
            contentType: "application/xml",
          },
        ],
      },
    };
    const prepared = runFunction("Alle XML/PDF-Kandidaten vorbereiten", msg).output;
    assert.equal(prepared[0], msg);
    Object.assign(msg, {
      payload: Buffer.from(REPORT_ARTIFACT.body),
      statusCode: 200,
      headers: {
        "content-type": REPORT_ARTIFACT.contentType,
        "x-einvoice-syntax": "CII",
        "x-einvoice-validation-status": "ok",
        "x-einvoice-official-status": "accepted",
      },
    });
    const classified = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
    assert.equal(classified[0], msg);
    return msg.automation.reports[0].filename;
  }

  const first = classifyMail("mail:alpha/ä");
  const second = classifyMail("mail:beta/ä");

  assert.notEqual(first, second);
  for (const filename of [first, second]) {
    assert.match(filename, /^E-Rechnungs-Pruefbericht-[A-Za-z0-9_-]+-1\.pdf$/);
    assert.equal(filename.includes("Original-Rechnung"), false);
    assert.equal(Buffer.byteLength(filename, "ascii"), filename.length);
  }
});

test("UNKNOWN syntax and all KoSIT availability states follow the agreed policy", () => {
  const unknown = apiResponse({ syntax: "UNKNOWN", validation: "invalid" });
  const unknownOutput = runFunction("HTTP- und Prüfstatus klassifizieren", unknown).output;
  assert.equal(unknownOutput[0], unknown);
  assert.equal(unknown.automation.results[0].outcome, "not-supported");
  assert.equal(unknown.automation.reports.length, 0);

  for (const official of ["not-requested", "unavailable"]) {
    const required = apiResponse({ official });
    const requiredOutput = runFunction("HTTP- und Prüfstatus klassifizieren", required).output;
    assert.equal(requiredOutput[2], required);
    assert.equal(required.automationError.class, "configuration");

    const optional = apiResponse({
      official,
      state: automationState({ requireKosit: false }),
    });
    const optionalOutput = runFunction("HTTP- und Prüfstatus klassifizieren", optional).output;
    assert.equal(optionalOutput[0], optional);
    assert.equal(optional.automation.results[0].outcome, "processed");
  }

  const indeterminateRequired = apiResponse({ official: "indeterminate" });
  const indeterminateOutput = runFunction(
    "HTTP- und Prüfstatus klassifizieren",
    indeterminateRequired,
  ).output;
  assert.equal(indeterminateOutput[1], indeterminateRequired);
  assert.match(indeterminateRequired.automation.lastError, /technisch unbestimmt/);

  const indeterminateOptional = apiResponse({
    official: "indeterminate",
    state: automationState({ requireKosit: false }),
  });
  const optionalOutput = runFunction(
    "HTTP- und Prüfstatus klassifizieren",
    indeterminateOptional,
  ).output;
  assert.equal(optionalOutput[0], indeterminateOptional);
  assert.equal(indeterminateOptional.automation.results[0].outcome, "processed");
});

test("HTTP input errors are terminal while transient statuses are retried", () => {
  for (const statusCode of [413, 422]) {
    const msg = apiResponse({ statusCode });
    const classified = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
    assert.equal(classified[0], msg);
    assert.equal(msg.automation.results[0].outcome, "input-error");
    assert.equal(msg.automation.results[0].httpStatus, statusCode);

    const selected = runFunction("Nächsten Kandidaten wählen", msg).output;
    assert.equal(selected[1], msg);
    assert.equal("requestTimeout" in msg, false);
    assert.equal("followRedirects" in msg, false);
  }

  for (const statusCode of [408, 429, 500, 503]) {
    const extraHeaders = statusCode === 429 ? { "retry-after": "7" } : {};
    const msg = apiResponse({ statusCode, extraHeaders });
    const classified = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
    assert.equal(classified[1], msg);

    const retried = runFunction("Begrenzt wiederholen", msg).output;
    assert.equal(retried[0], msg);
    assert.equal(msg.delay, statusCode === 429 ? 7000 : 30000);
    assert.equal("requestTimeout" in msg, false);
    assert.equal("followRedirects" in msg, false);
  }
});

test("Retry-After accepts seconds and HTTP dates but never delays beyond ten minutes", () => {
  const cases = [
    { value: "0", expected: 0 },
    { value: "999999", expected: 600000 },
    { value: new Date(Date.now() + 60 * 60 * 1000).toUTCString(), expected: 600000 },
    { value: "kein gueltiger Wert", expected: 30000 },
  ];

  for (const { value, expected } of cases) {
    const msg = apiResponse({ statusCode: 429, extraHeaders: { "retry-after": value } });
    const classified = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
    assert.equal(classified[1], msg);
    const retried = runFunction("Begrenzt wiederholen", msg).output;
    assert.equal(retried[0], msg);
    assert.equal(msg.delay, expected);
  }
});

test("HTTP catch diagnostics survive retries without leaking msg.error", () => {
  const msg = {
    automation: automationState(),
    error: { message: "connect ECONNREFUSED 127.0.0.1:8080", code: "ECONNREFUSED" },
    requestTimeout: 90000,
    followRedirects: false,
  };

  const { output } = runFunction("Begrenzt wiederholen", msg);

  assert.equal(output[0], msg);
  assert.match(msg.automation.lastError, /ECONNREFUSED/);
  assert.equal(msg.delay, 30000);
  assert.equal("error" in msg, false);
  assert.equal("requestTimeout" in msg, false);
  assert.equal("followRedirects" in msg, false);
});

test("retry backoff is bounded and exhaustion stays on the non-ACK error path", () => {
  const msg = {
    automation: automationState(),
    requestTimeout: 90000,
  };
  msg.automation.lastError = "temporärer Fehler";

  for (const expectedDelay of [30000, 120000, 600000]) {
    const { output } = runFunction("Begrenzt wiederholen", msg);
    assert.equal(output[0], msg);
    assert.equal(output[1], null);
    assert.equal(msg.delay, expectedDelay);
    assert.equal("requestTimeout" in msg, false);
    assert.equal("followRedirects" in msg, false);
    msg.requestTimeout = 90000;
    msg.followRedirects = false;
  }

  const { output } = runFunction("Begrenzt wiederholen", msg);
  assert.equal(output[0], null);
  assert.equal(output[1], msg);
  assert.equal(msg.automationError.class, "retry-exhausted");
  assertIsoTimestamp(msg.automationError.occurredAt);
  assert.equal("requestTimeout" in msg, false);
  assert.equal("followRedirects" in msg, false);

  const retryNode = functionNode("Begrenzt wiederholen");
  const cleaner = byName.get("Technischen Fehler bereinigen");
  assert.deepEqual(retryNode.wires[1], [cleaner.id]);
});

test("multiple reports become mail attachments while IMAP ACK context survives", () => {
  const candidates = [candidate("eins.xml"), candidate("zwei.xml")];
  const state = automationState({ candidates });
  const imap = imapContext(99);
  const originalAttachments = [
    { filename: "eins.xml", content: candidates[0].content, contentType: "application/xml" },
    { filename: "zwei.xml", content: candidates[1].content, contentType: "application/xml" },
  ];
  const msg = apiResponse({ state });
  msg.imap = imap;
  msg.email = {
    topic: "Eingangsrechnung",
    header: { "reply-to": "eingang@example.invalid" },
    attachments: originalAttachments,
  };

  let output = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
  assert.equal(output[0], msg);
  output = runFunction("Nächsten Kandidaten wählen", msg).output;
  assert.equal(output[0], msg);
  assert.equal(msg.automation.candidateIndex, 1);

  Object.assign(msg, {
    payload: Buffer.from(REPORT_ARTIFACT.body),
    statusCode: 200,
    requestTimeout: 90000,
    headers: {
      "content-type": REPORT_ARTIFACT.contentType,
      "x-einvoice-syntax": "UBL",
      "x-einvoice-validation-status": "warning",
      "x-einvoice-official-status": "rejected",
    },
  });
  output = runFunction("HTTP- und Prüfstatus klassifizieren", msg).output;
  assert.equal(output[0], msg);
  output = runFunction("Nächsten Kandidaten wählen", msg).output;
  assert.equal(output[1], msg);

  output = runFunction("Mailergebnis abschließen", msg, {
    EINVOICE_RESULT_TO: "result@example.invalid",
  }).output;
  assert.equal(output[0], msg);
  assert.equal(output[1], null);
  assert.equal(output[2], null);
  assert.equal(msg.automationRecipient, "result@example.invalid");
  assert.equal(msg.email.attachments.length, 4);
  assert.deepEqual(
    msg.email.attachments.slice(2).map((item) => item.contentType),
    [REPORT_ARTIFACT.contentType, REPORT_ARTIFACT.contentType],
  );
  assert.deepEqual(
    msg.email.attachments.slice(2).map((item) => item.contentDisposition),
    [REPORT_ARTIFACT.contentDisposition, REPORT_ARTIFACT.contentDisposition],
  );
  const reportNames = msg.email.attachments.slice(2).map((item) => item.filename);
  assert.ok(reportNames.every((filename) => filename.endsWith(REPORT_ARTIFACT.extension)));
  assert.match(reportNames[0], /-1\.pdf$/);
  assert.match(reportNames[1], /-2\.pdf$/);
  assert.equal(reportNames[0].replace(/-1\.pdf$/, ""), reportNames[1].replace(/-2\.pdf$/, ""));
  assert.match(msg.email.text, /eins\.xml: CII/);
  assert.match(msg.email.text, /zwei\.xml: UBL/);
  assert.match(msg.email.text, /PDF-Prüfberichte/);
  assert.match(msg.email.text, /Darstellungsgrenzen/);
  assert.strictEqual(msg.imap, imap);
  assert.strictEqual(msg.imap.ackToken, imap.ackToken);
  assert.equal("requestTimeout" in msg, false);
  assert.equal("followRedirects" in msg, false);

  const mailOptions = executeSmtpMappings(msg);
  assert.equal(mailOptions.to, "result@example.invalid");
  assert.equal(mailOptions.from, "rechnung@example.invalid");
  assert.equal(mailOptions.replyTo, "eingang@example.invalid");
  assert.equal(mailOptions.subject, msg.email.topic);
  assert.equal(mailOptions.text, msg.email.text);
  assert.strictEqual(mailOptions.attachments, msg.email.attachments);
  assert.deepEqual(
    mailOptions.attachments.slice(2).map((item) => ({
      filename: item.filename,
      contentType: item.contentType,
      contentDisposition: item.contentDisposition,
      signature: item.content.subarray(0, 5).toString("ascii"),
    })),
    msg.automation.reports.map((report) => ({
      filename: report.filename,
      contentType: "application/pdf",
      contentDisposition: "attachment",
      signature: "%PDF-",
    })),
  );
});

test("SMTP success is the only report path to ACK; errors never acknowledge", () => {
  const finalizer = functionNode("Mailergebnis abschließen");
  const smtp = byName.get("Prüfbericht versenden");
  const ack = byName.get("Erst jetzt quittieren");
  const cleaner = byName.get("Technischen Fehler bereinigen");
  const httpCatch = byName.get("HTTP-Verbindungsfehler");
  const httpRequest = byName.get("POST /api/report/pdf");
  const retry = functionNode("Begrenzt wiederholen");

  assert.ok(httpRequest);
  assert.equal("requestTimeout" in httpRequest, false);
  assert.deepEqual(finalizer.wires, [[smtp.id], [ack.id], [cleaner.id]]);
  assert.deepEqual(smtp.wires, [[ack.id], [cleaner.id]]);
  assert.deepEqual(ack.wires[1], [cleaner.id]);
  assert.equal(smtp.wires[1].includes(ack.id), false);
  assert.equal(ack.wires[1].includes(ack.id), false);
  assert.deepEqual(httpCatch.wires, [[retry.id]]);

  const noReports = {
    automation: automationState(),
    imap: imapContext(77),
    requestTimeout: 90000,
    followRedirects: false,
  };
  const noReportsOutput = runFunction("Mailergebnis abschließen", noReports).output;
  assert.equal(noReportsOutput[0], null);
  assert.equal(noReportsOutput[1], noReports);
  assert.equal(noReportsOutput[2], null);
  assert.equal(noReports.automation.outcome, "not-supported");
  assert.equal("requestTimeout" in noReports, false);
  assert.equal("followRedirects" in noReports, false);

  const missingRecipient = {
    automation: automationState(),
    imap: imapContext(78),
  };
  missingRecipient.automation.reports.push({
    filename: `report${REPORT_ARTIFACT.extension}`,
    content: REPORT_ARTIFACT.body,
    contentDisposition: REPORT_ARTIFACT.contentDisposition,
    contentType: REPORT_ARTIFACT.contentType,
    sourceFilename: "rechnung.xml",
    syntax: "CII",
    validation: "ok",
    official: "accepted",
  });
  const missingRecipientOutput = runFunction("Mailergebnis abschließen", missingRecipient).output;
  assert.equal(missingRecipientOutput[0], null);
  assert.equal(missingRecipientOutput[1], null);
  assert.equal(missingRecipientOutput[2], missingRecipient);
  assert.equal(missingRecipient.automationError.class, "configuration");
  assertIsoTimestamp(missingRecipient.automationError.occurredAt);
});

test("technical cleanup removes HTTP transport state but keeps diagnostics and ACK context", () => {
  const imap = imapContext(88);
  const msg = {
    _msgid: "error-88",
    automation: automationState(),
    imap,
    error: { message: "socket closed" },
    payload: Buffer.from("secret invoice bytes"),
    headers: { Authorization: "Bearer secret" },
    statusCode: 500,
    url: "http://127.0.0.1:8080/api/report/pdf",
    method: "POST",
    requestTimeout: 90000,
    followRedirects: false,
  };
  const { output, statuses } = runFunction("Technischen Fehler bereinigen", msg);

  assert.equal(output, msg);
  assert.equal(msg.automationError.class, "runtime");
  assert.equal(msg.automationError.message, "socket closed");
  assertIsoTimestamp(msg.automationError.occurredAt);
  assert.strictEqual(msg.imap, imap);
  assert.strictEqual(msg.imap.ackToken, imap.ackToken);
  for (const key of [
    "payload",
    "headers",
    "statusCode",
    "url",
    "method",
    "requestTimeout",
    "followRedirects",
    "error",
  ]) {
    assert.equal(key in msg, false, `${key} wurde nicht bereinigt`);
  }
  assert.deepEqual(
    statuses.map((status) => ({ ...status })),
    [{ fill: "red", shape: "ring", text: "runtime" }],
  );
});

test("oversized IMAP messages remain identifiable on the manual non-ACK path", () => {
  const imap = imapContext(671);
  const msg = {
    _msgid: "too-large-671",
    imap,
    error: {
      code: "IMAP_EMAIL_MESSAGE_TOO_LARGE",
      message: "Message exceeds configured maximum of 67108864 bytes",
    },
  };

  const { output } = runFunction("Technischen Fehler bereinigen", msg);

  assert.equal(output, msg);
  assert.equal(msg.automationError.class, "runtime");
  assert.equal(msg.automationError.code, "IMAP_EMAIL_MESSAGE_TOO_LARGE");
  assertIsoTimestamp(msg.automationError.occurredAt);
  assert.strictEqual(msg.imap.ackToken, imap.ackToken);
  assert.equal("error" in msg, false);

  const persistentError = byName.get("TECHNISCHER FEHLER – persistent anbinden");
  const acknowledgement = byName.get("Erst jetzt quittieren");
  assert.deepEqual(persistentError.links, []);
  assert.equal(persistentError.links.includes(acknowledgement.id), false);
});
