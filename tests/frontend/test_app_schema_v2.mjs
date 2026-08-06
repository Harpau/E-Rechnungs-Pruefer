import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const scriptUrl = new URL('../../app/static/app.js', import.meta.url);
const scriptSource = fs.readFileSync(scriptUrl, 'utf8');
const templateUrl = new URL('../../app/templates/index.html', import.meta.url);
const templateSource = fs.readFileSync(templateUrl, 'utf8');
const stylesUrl = new URL('../../app/static/styles.css', import.meta.url);
const stylesSource = fs.readFileSync(stylesUrl, 'utf8');

function rendererContext() {
  const elements = new Map();
  const makeElement = (className = '') => {
    const attributes = new Map();
    const node = {
      textContent: '',
      innerHTML: '',
      hidden: false,
      className,
      value: '',
      checked: false,
      disabled: false,
      previousElementSibling: null,
      addEventListener() {},
      setAttribute(name, value) { attributes.set(name, String(value)); },
      getAttribute(name) { return attributes.get(name) ?? null; },
    };
    const classTokens = () => new Set(node.className.split(/\s+/).filter(Boolean));
    const writeClassTokens = (tokens) => { node.className = [...tokens].join(' '); };
    node.classList = {
      add(...names) {
        const tokens = classTokens();
        names.forEach((name) => tokens.add(name));
        writeClassTokens(tokens);
      },
      remove(...names) {
        const tokens = classTokens();
        names.forEach((name) => tokens.delete(name));
        writeClassTokens(tokens);
      },
      contains(name) { return classTokens().has(name); },
      toggle(name, force) {
        const tokens = classTokens();
        const present = tokens.has(name);
        const enabled = force === undefined ? !present : Boolean(force);
        if (enabled) tokens.add(name);
        else tokens.delete(name);
        writeClassTokens(tokens);
        return enabled;
      },
    };
    return node;
  };
  const exampleButtons = [makeElement('example-button'), makeElement('example-button')];
  const element = (selector) => {
    if (!elements.has(selector)) {
      elements.set(selector, makeElement(selector === '.upload-card' ? 'upload-card' : ''));
    }
    return elements.get(selector);
  };
  element('#payable-total').previousElementSibling = makeElement();

  const document = {
    querySelector: element,
    querySelectorAll(selector) {
      return selector === '.example-button' ? exampleButtons : [];
    },
    addEventListener() {},
  };
  const context = vm.createContext({ document });
  vm.runInContext(scriptSource, context, { filename: scriptUrl.pathname });
  return { context, element, exampleButtons };
}

function schemaTwoPayload() {
  const emptyCounts = { error: 0, warning: 0, info: 0 };
  return {
    schema_version: 2,
    document: {
      id: 'SYNTHETIC-389',
      issue_date: '2026-07-31',
      type: {
        status: 'known',
        code: { value: '389', label: 'Eigenabrechnung', list_id: 'UNCL1001' },
        family: 'invoice',
        base_polarity: 'debit',
        settlement_relevance: 'relevant',
        self_billing: true,
        ubl_root: 'invoice',
        root_compatibility: 'compatible',
        registry_version: '2026-07',
      },
      tax_point_date: null,
      tax_point_date_code: null,
      document_currency: { value: 'EUR', label: 'Euro', list_id: 'ISO4217' },
      vat_accounting_currency: null,
      buyer_reference: 'BUYER-REF',
      notes: [{ text: 'Synthetischer Hinweis', subject_code: null }],
    },
    profile: { id: 'urn:synthetic', name: 'Synthetisches Profil', business_process_id: null },
    capabilities: {
      syntax: 'UBL',
      syntax_version: '2.1',
      format_name: 'OASIS UBL 2.1 Invoice',
      document_type_recognition: 'recognized',
      rendering: 'full',
      internal_checks: 'full',
      official_validation: 'bundled',
    },
    parties: {
      seller: { legal_name: 'Synthetischer Verkäufer' },
      buyer: { legal_name: 'Synthetischer Käufer' },
      payee: null,
      invoice_recipient: null,
      seller_tax_representative: null,
      delivery_recipient: null,
    },
    roles: {
      issuer: 'buyer',
      document_recipient: 'seller',
      creditor: 'seller',
      debtor: 'buyer',
      expected_payer: 'buyer',
      expected_recipient: 'seller',
      expected_payment_direction: 'debtor-to-creditor',
      derivation: 'derived',
    },
    periods: {
      invoice: { start_date: '2026-07-01', end_date: '2026-07-31', description: null },
      delivery: null,
    },
    delivery: {
      actual_date: '2026-07-20',
      location: {
        id: { value: '4000001000005', scheme_id: 'GLN' },
        postal_address: {
          line1: 'Lieferweg 1',
          line2: null,
          line3: null,
          postcode: '12345',
          city: 'Lieferstadt',
          subdivision: null,
          country: { value: 'DE', label: 'Deutschland', list_id: 'ISO3166-1' },
        },
      },
    },
    references: {
      buyer_order: null,
      seller_order: null,
      contract: null,
      tender: null,
      project: null,
      buyer_accounting_reference: null,
      invoiced_object: null,
      preceding_invoices: [],
      supporting_documents: [],
      despatch_advice: null,
      receiving_advice: null,
    },
    lines: [{
      id: '1',
      notes: [],
      item: { name: 'Synthetische Leistung', classifications: [], properties: [] },
      quantity: { value: '1', unit: { value: 'C62', label: 'Stück', list_id: 'UNECERec20' } },
      period: null,
      order_line_reference: null,
      accounting_reference: null,
      object_identifier: null,
      price: { net: { value: '100.00', currency: 'EUR' }, base_quantity: null },
      allowances_charges: [],
      tax_type: { value: 'VAT', label: null, list_id: null },
      tax_category: { value: 'S', label: 'Standardsteuersatz', list_id: null },
      tax_rate_percent: '19',
      net_amount: { value: '100.00', currency: 'EUR' },
    }],
    allowances_charges: [],
    tax: {
      breakdown: [],
      totals: { document_currency: { value: '19.00', currency: 'EUR' }, vat_accounting_currency: null },
    },
    totals: {
      line_net_total: { value: '100.00', currency: 'EUR' },
      allowance_total: null,
      charge_total: null,
      tax_exclusive_total: { value: '100.00', currency: 'EUR' },
      tax_inclusive_total: { value: '119.00', currency: 'EUR' },
      prepaid_total: null,
      rounding: null,
      payable: { value: '119.00', currency: 'EUR' },
    },
    payment: {
      due_date: '2026-08-14',
      reference: 'PAY-REF',
      terms: [],
      instructions: [{
        means: { value: '48', label: 'Zahlungskarte', list_id: null },
        instruction_note: null,
        payment_id: null,
        credit_transfers: [],
        payment_card: {
          masked_account_identifier: '4111111111111234',
          holder_name: 'Synthetische Karteninhaberin',
        },
        direct_debit: null,
      }],
    },
    assessment: {
      official: {
        status: 'not-requested',
        requested: false,
        executed: false,
        summary: 'Nicht angefordert.',
        findings: [],
        counts: emptyCounts,
      },
      internal: {
        status: 'attention',
        executed: true,
        summary: 'Zahlungsangaben prüfen.',
        scope: 'Interne Plausibilitätsprüfung.',
        findings: [{
          origin: 'internal',
          rule_class: 'plausibility',
          severity: 'warning',
          rule: {
            id: 'PAY-001',
            title: 'Zahlungsangaben prüfen',
            message: 'Die Zahlungsangaben passen möglicherweise nicht zum Geschäftsvorfall.',
            source: 'E-Rechnungs-Prüfer',
          },
          semantic_references: [{ id: 'BG-16', label: 'Zahlungsanweisungen' }],
          occurrence: {
            scope: 'payment',
            index: 0,
            identifier: null,
            json_pointer: '/payment/instructions/0',
          },
          xml_location: {
            path: '/Invoice/PaymentMeans',
            line: 42,
            column: null,
          },
          actual: { value: 'keine Zahlungsanweisung', data_type: 'text', unit: null },
          expected: { value: 'profilabhängig prüfen', data_type: 'text', unit: null },
        }],
        counts: { error: 0, warning: 1, info: 0 },
      },
      processing: {
        status: 'complete',
        summary: 'Vollständig verarbeitet.',
        limitations: [],
        findings: [],
        counts: emptyCounts,
      },
    },
    source: {
      upload: null,
      invoice_xml: null,
      container: { kind: 'xml', page_count: null, selected_attachment: null, attachment_count: 0 },
      attachments: [],
    },
    technical: {
      root_element: 'Invoice',
      root_namespace: 'urn:synthetic',
      field_count: 1,
      truncated: false,
      fields: [{ kind: 'element', path: '/Invoice/ID', name: 'ID', namespace: null, value: 'SYNTHETIC-389' }],
      source_xml: '<Invoice />',
      pretty_xml: '<Invoice/>\n',
    },
    runtime: {
      generated_at: '2026-07-31T12:00:00Z',
      duration_ms: '12.3',
      application_version: '2.0.0',
    },
  };
}

test('Ladezustand schaltet Compositing und Bedienelemente vollständig um', () => {
  const { context, element, exampleButtons } = rendererContext();

  context.setLoading(true);
  assert.equal(element('#progress').hidden, false);
  assert.equal(element('#drop-zone').getAttribute('aria-busy'), 'true');
  assert.equal(element('#file-input').disabled, true);
  assert.equal(element('.upload-card').classList.contains('is-loading'), true);
  assert.equal(element('.upload-card').classList.contains('upload-card'), true);
  assert.equal(exampleButtons.every((button) => button.disabled), true);

  context.setLoading(false);
  assert.equal(element('#progress').hidden, true);
  assert.equal(element('#drop-zone').getAttribute('aria-busy'), 'false');
  assert.equal(element('#file-input').disabled, false);
  assert.equal(element('.upload-card').classList.contains('is-loading'), false);
  assert.equal(element('.upload-card').classList.contains('upload-card'), true);
  assert.equal(exampleButtons.every((button) => !button.disabled), true);
});

test('Analysefehler räumt den Ladezustand im finally-Block auf', async () => {
  const { context, element, exampleButtons } = rendererContext();
  context.FormData = class {
    append() {}
  };
  vm.runInContext(
    "globalThis.fetch = async () => { throw new Error('Synthetischer Netzwerkfehler'); };",
    context,
  );

  await context.analyzeFile({ name: 'synthetic.xml' });

  assert.equal(element('#error-box').hidden, false);
  assert.equal(element('#error-box').textContent, 'Synthetischer Netzwerkfehler');
  assert.equal(element('#progress').hidden, true);
  assert.equal(element('#drop-zone').getAttribute('aria-busy'), 'false');
  assert.equal(element('#file-input').disabled, false);
  assert.equal(element('.upload-card').classList.contains('is-loading'), false);
  assert.equal(exampleButtons.every((button) => !button.disabled), true);
});

test('Schema-2 payload renders axes, semantic references and masked cards', () => {
  const { context, element } = rendererContext();
  context.renderAll(schemaTwoPayload());

  const factsHtml = element('#document-facts').innerHTML;
  const paymentHtml = element('#payment-section').innerHTML;
  assert.equal(element('#document-title').textContent, 'Rechnung SYNTHETIC-389');
  assert.equal(element('#document-type-summary').textContent, 'Rechnungsart · 389 – Eigenabrechnung');
  assert.equal(element('#payable-total').previousElementSibling.textContent, 'Ausstehender Betrag (BT-115)');
  assert.equal((factsHtml.match(/class="fact"/g) || []).length, 30);
  assert.doesNotMatch(factsHtml, /Typregister-Version/);
  assert.match(paymentHtml, /<section class="payment-section payment-flow-section">/);
  assert.match(paymentHtml, /<h3 class="payment-heading payment-section-heading">Dokument- und Zahlungsfluss<\/h3>/);
  assert.match(paymentHtml, /<section class="payment-section payment-instructions-section">/);
  assert.match(paymentHtml, /<article class="payment-item">/);
  assert.match(paymentHtml, /<h4 class="payment-heading payment-item-heading">Zahlungsanweisung 1<\/h4>/);
  assert.match(paymentHtml, /<h5 class="payment-heading payment-detail-heading">Zahlungskarte<\/h5>/);
  assert.doesNotMatch(paymentHtml, /class="subsection-heading"/);
  assert.match(paymentHtml, /Dokument- und Zahlungsfluss/);
  assert.match(paymentHtml, /<dt>Dokumentfluss<\/dt>/);
  assert.match(paymentHtml, /<dt>Erwarteter Zahlungsfluss<\/dt>/);
  assert.equal((paymentHtml.match(/Käufer → Verkäufer/g) || []).length, 2);
  assert.doesNotMatch(paymentHtml, /<dt>Gläubiger<\/dt>/);
  assert.doesNotMatch(paymentHtml, /<dt>Schuldner<\/dt>/);
  assert.doesNotMatch(paymentHtml, /<dt>Herleitung<\/dt>/);
  assert.doesNotMatch(paymentHtml, /<dt>Erwarteter Zahler<\/dt>/);
  assert.match(paymentHtml, /Zahlungsanweisungen \(BG-16\)/);
  assert.match(paymentHtml, /•••• 1234/);
  assert.doesNotMatch(paymentHtml, /4111111111111234/);
  assert.match(paymentHtml, /kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss/);
  assert.match(element('#findings-list').innerHTML, /Zahlungsanweisungen \(BG-16\)/);
  assert.match(element('#findings-list').innerHTML, /XML-Pfad: \/Invoice\/PaymentMeans/);
  assert.doesNotMatch(element('#findings-list').innerHTML, /Ort:/);
  assert.match(element('#official-state').innerHTML, /Offizielle Konformitätsprüfung/);
  assert.match(element('#official-state').innerHTML, /Interne Prüfung/);
  assert.match(element('#official-state').innerHTML, /Verarbeitung/);
  assert.match(element('#document-facts').innerHTML, /Tatsächliches Lieferdatum \(BT-72\)/);
  assert.match(element('#document-facts').innerHTML, /4000001000005 \(GLN\)/);
  assert.match(element('#document-facts').innerHTML, /Lieferweg 1, 12345 Lieferstadt, DE/);
});

test('USt-Zellen priorisieren den Steuersatz und bleiben vollständig zugänglich', () => {
  const payload = schemaTwoPayload();
  payload.tax.breakdown = [{
    category: { value: 'S', label: 'Standardsteuersatz', list_id: null },
    rate_percent: '19.000',
    tax_type: { value: 'VAT', label: null, list_id: null },
    taxable_amount: { value: '100.00', currency: 'EUR' },
    tax_amount: { value: '19.00', currency: 'EUR' },
    exemption: null,
  }];

  const { context, element } = rendererContext();
  context.renderAll(payload);

  const linesHtml = element('#line-items-body').innerHTML;
  const ratePosition = linesHtml.indexOf('line-tax-rate');
  const codePosition = linesHtml.indexOf('class="line-tax-code"');
  assert.ok(ratePosition >= 0 && codePosition > ratePosition);
  assert.match(linesHtml, /<strong class="line-tax-primary line-tax-rate">19&nbsp;%<\/strong>/);
  assert.match(linesHtml, /<span class="line-tax-code">S<\/span>/);
  assert.match(linesHtml, /<td class="num"><strong>100,00 €<\/strong><\/td>/);
  assert.match(
    linesHtml,
    /Steuersatz 19 Prozent, Steuerkategorie S, Standardsteuersatz, Steuerart VAT/,
  );
  assert.doesNotMatch(linesHtml, />S – Standardsteuersatz · 19/);
  assert.equal(element('#line-tax-breakdown-notices').hidden, true);
  assert.equal(element('#line-tax-breakdown-notices').innerHTML, '');
});

test('USt-Zellen zeigen unterschiedlich lange Steuersätze ohne Float-Rundung vollständig an', () => {
  const payload = schemaTwoPayload();
  payload.lines = [
    { ...payload.lines[0], id: 'PRECISE-1', tax_rate_percent: '19.123456' },
    { ...payload.lines[0], id: 'PRECISE-2', tax_rate_percent: '19.1234567' },
  ];
  payload.tax.breakdown = [
    { category: { value: 'S', label: 'Standardsteuersatz' }, rate_percent: '19.123456' },
  ];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const linesHtml = element('#line-items-body').innerHTML;

  assert.match(linesHtml, /19,123456&nbsp;%/);
  assert.match(linesHtml, /19,1234567&nbsp;%/);
  assert.doesNotMatch(linesHtml, /19,1235&nbsp;%/);
  assert.equal(element('#line-tax-breakdown-notices').hidden, false);
  assert.match(element('#line-tax-breakdown-notices').innerHTML, /19,1234567&nbsp;%/);
  assert.doesNotMatch(element('#line-tax-breakdown-notices').innerHTML, /19,123456&nbsp;%/);
});

test('USt-Zellen unterscheiden fehlende Sätze, Kategorien und vollständig fehlende Angaben', () => {
  const payload = schemaTwoPayload();
  payload.lines = [
    {
      ...payload.lines[0],
      id: 'O',
      tax_category: { value: 'O', label: 'Nicht der Umsatzsteuer unterliegend', list_id: null },
      tax_rate_percent: null,
    },
    {
      ...payload.lines[0],
      id: 'S',
      tax_category: { value: 'S', label: 'Standardsteuersatz', list_id: null },
      tax_rate_percent: null,
    },
    {
      ...payload.lines[0],
      id: 'RATE',
      tax_category: null,
      tax_rate_percent: '7.00',
    },
    {
      ...payload.lines[0],
      id: 'EMPTY',
      tax_category: null,
      tax_rate_percent: null,
      tax_type: null,
    },
  ];
  payload.tax.breakdown = [
    { category: { value: 'O', label: null }, rate_percent: null },
    { category: { value: 'S', label: null }, rate_percent: null },
  ];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const linesHtml = element('#line-items-body').innerHTML;

  assert.match(
    linesHtml,
    /<strong class="line-tax-primary line-tax-primary-code">O<\/strong>[\s\S]*?<span class="line-tax-status">ohne Steuersatz<\/span>/,
  );
  assert.match(
    linesHtml,
    /<strong class="line-tax-primary line-tax-primary-code">S<\/strong>[\s\S]*?<span class="line-tax-status">Steuersatz nicht angegeben<\/span>/,
  );
  assert.match(
    linesHtml,
    /<strong class="line-tax-primary line-tax-rate">7&nbsp;%<\/strong>[\s\S]*?<span class="line-tax-status">Kategorie nicht angegeben<\/span>/,
  );
  assert.match(linesHtml, /<span class="line-tax-empty">–<\/span>/);
  assert.doesNotMatch(linesHtml, /<strong[^>]*>–<\/strong>/);
  assert.match(linesHtml, /Steuerkategorie O, Nicht der Umsatzsteuer unterliegend, ohne Steuersatz/);
  assert.match(linesHtml, /Steuersatz 7 Prozent, Steuerkategorie nicht angegeben/);
  assert.match(linesHtml, /Steuersatz und Steuerkategorie nicht angegeben/);
});

test('USt-Zelle und Abweichungshinweis umbrechen einen langen unbekannten Kategoriecode sicher', () => {
  const payload = schemaTwoPayload();
  const longCode = 'X'.repeat(200);
  payload.lines[0].tax_category = { value: longCode, label: null };
  payload.lines[0].tax_rate_percent = null;
  payload.tax.breakdown = [];

  const { context, element } = rendererContext();
  context.renderAll(payload);

  assert.match(
    element('#line-items-body').innerHTML,
    new RegExp(`<strong class="line-tax-primary line-tax-primary-code">${longCode}<\\/strong>`),
  );
  assert.match(element('#line-tax-breakdown-notices').innerHTML, new RegExp(longCode));
  assert.equal(element('#line-tax-breakdown-notices').hidden, false);
});

test('Hinweis meldet nur fehlende Kategorie-Satz-Kombinationen und dedupliziert Dezimalwerte', () => {
  const payload = schemaTwoPayload();
  payload.lines = [
    { ...payload.lines[0], id: '1', tax_rate_percent: '19' },
    { ...payload.lines[0], id: '2', tax_rate_percent: '19.00' },
    {
      ...payload.lines[0],
      id: '3',
      tax_category: { value: 'AE', label: 'Steuerschuldnerschaft des Leistungsempfängers' },
      tax_rate_percent: '0',
    },
    {
      ...payload.lines[0],
      id: '4',
      tax_category: { value: 'AE', label: 'Steuerschuldnerschaft des Leistungsempfängers' },
      tax_rate_percent: '0.000',
    },
  ];
  payload.tax.breakdown = [{
    category: { value: 'S', label: 'Standardsteuersatz' },
    rate_percent: '19.0000',
  }];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const notice = element('#line-tax-breakdown-notices');

  assert.equal(notice.hidden, false);
  assert.equal((notice.innerHTML.match(/AE – Steuerschuldnerschaft/g) || []).length, 1);
  assert.match(notice.innerHTML, /0&nbsp;%/);
  assert.doesNotMatch(notice.innerHTML, /S – Standardsteuersatz/);
  assert.doesNotMatch(notice.innerHTML, /Steuerkategorien:/);
  assert.match(notice.innerHTML, /Nicht in der Steueraufschlüsselung enthalten:/);
});

test('Hinweis erkennt einen abweichenden Satz und meldet kategorielose Sätze, aber keine leeren Kombinationen', () => {
  const payload = schemaTwoPayload();
  payload.lines = [
    { ...payload.lines[0], id: 'DIFFERENT-RATE', tax_rate_percent: '7.0' },
    { ...payload.lines[0], id: 'NO-CATEGORY', tax_category: null, tax_rate_percent: '19' },
    { ...payload.lines[0], id: 'NO-TAX-DATA', tax_category: null, tax_rate_percent: null },
  ];
  payload.tax.breakdown = [{
    category: { value: 'S', label: 'Standardsteuersatz' },
    rate_percent: '19.00',
  }];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const noticeHtml = element('#line-tax-breakdown-notices').innerHTML;

  assert.equal((noticeHtml.match(/S – Standardsteuersatz/g) || []).length, 1);
  assert.match(noticeHtml, /7&nbsp;%/);
  assert.match(noticeHtml, /Kategorie nicht angegeben · 19&nbsp;%/);
  assert.equal((noticeHtml.match(/Kategorie nicht angegeben/g) || []).length, 1);
  assert.doesNotMatch(noticeHtml, /Steuersatz nicht angegeben/);
});

test('Hinweis erkennt bereits präfixierte Kategorien case-insensitiv und bewahrt den Rohcode', () => {
  const payload = schemaTwoPayload();
  payload.lines = [{
    ...payload.lines[0],
    tax_category: { value: 's', label: 'S – Standardsteuersatz' },
    tax_rate_percent: '19',
  }];
  payload.tax.breakdown = [];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const linesHtml = element('#line-items-body').innerHTML;
  const noticeHtml = element('#line-tax-breakdown-notices').innerHTML;

  assert.match(linesHtml, /<span class="line-tax-code">s<\/span>/);
  assert.match(linesHtml, /Steuerkategorie s, Standardsteuersatz/);
  assert.match(noticeHtml, /s – Standardsteuersatz · 19&nbsp;%/);
  assert.doesNotMatch(noticeHtml, /s – S – Standardsteuersatz/);
  assert.match(noticeHtml, /<\/strong>\s+<ul>/);
});

test('Positionslayout reserviert Platz für USt und druckt den Abweichungshinweis mit', () => {
  assert.match(templateSource, /id="line-tax-breakdown-notices"/);
  assert.match(stylesSource, /\.line-table\s*\{[^}]*table-layout:\s*fixed/s);
  assert.match(stylesSource, /\.line-table\s+col\.line-tax-column\s*\{[^}]*width:\s*11%/s);
  assert.match(stylesSource, /\.line-tax-rate\s*\{[^}]*white-space:\s*nowrap/s);
  assert.match(stylesSource, /\.line-tax-code\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(stylesSource, /\.line-tax-code\s*\{[^}]*white-space:\s*normal/s);
  assert.match(stylesSource, /\.line-tax-code\s*\{[^}]*font-size:\s*\.7rem/s);
  assert.match(stylesSource, /\.line-tax-status\s*\{[^}]*font-size:\s*\.65rem/s);
  assert.match(stylesSource, /\.line-tax-primary-code\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(stylesSource, /\.line-tax-primary-code\s*\{[^}]*white-space:\s*normal/s);
  const primaryRule = stylesSource.match(/\.line-tax-primary\s*\{([^}]*)\}/s)?.[1] || '';
  const rateRule = stylesSource.match(/\.line-tax-rate\s*\{([^}]*)\}/s)?.[1] || '';
  assert.doesNotMatch(primaryRule, /font-(?:size|weight)/);
  assert.doesNotMatch(rateRule, /font-(?:size|weight)/);
  assert.match(stylesSource, /\.line-tax-breakdown-notice\s*\{[^}]*max-width:\s*100%/s);
  assert.match(stylesSource, /\.line-tax-breakdown-notice\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(stylesSource, /\.line-tax-breakdown-notice ul\s*\{[^}]*margin:\s*0 0 0 [1-9]/s);
  assert.match(stylesSource, /@media print[\s\S]*\.line-table\s*\{[^}]*min-width:\s*0/s);
  assert.match(stylesSource, /@media print[\s\S]*\.line-tax-code,\s*\.line-tax-status\s*\{[^}]*font-size:\s*7pt/s);
  assert.doesNotMatch(stylesSource, /@media print[\s\S]*\.line-tax-rate\s*\{[^}]*font-size/s);
  assert.match(stylesSource, /@media print[\s\S]*\.line-tax-breakdown-notice\s*\{[^}]*break-inside:\s*avoid/s);
});

test('Kopfzeile kennzeichnet unbekannte und fehlende Rechnungsarten ausdrücklich', () => {
  const unknown = schemaTwoPayload();
  unknown.document.type = {
    ...unknown.document.type,
    status: 'unknown',
    code: { value: '999', label: null, list_id: 'UNCL1001' },
    family: 'unknown',
  };
  const unknownRenderer = rendererContext();
  unknownRenderer.context.renderAll(unknown);
  assert.equal(
    unknownRenderer.element('#document-type-summary').textContent,
    'Rechnungsart · 999 – Unbekannter Dokumenttyp',
  );

  const missing = schemaTwoPayload();
  missing.document.type = {
    ...missing.document.type,
    status: 'missing',
    code: null,
    family: 'unknown',
  };
  const missingRenderer = rendererContext();
  missingRenderer.context.renderAll(missing);
  assert.equal(
    missingRenderer.element('#document-type-summary').textContent,
    'Rechnungsart · Nicht angegeben',
  );
});

test('Gesamtstatus zeigt alle drei Texte und priorisiert Handlungsbedarf', () => {
  const clear = schemaTwoPayload();
  clear.assessment.official.status = 'accepted';
  clear.assessment.internal.status = 'clear';
  clear.assessment.processing.status = 'complete';

  const warning = schemaTwoPayload();
  warning.assessment.official.status = 'accepted';
  warning.assessment.internal.status = 'attention';
  warning.assessment.processing.status = 'complete';

  const invalid = schemaTwoPayload();
  invalid.assessment.official.status = 'rejected';
  invalid.assessment.internal.status = 'attention';
  invalid.assessment.processing.status = 'limited';

  for (const [payload, label, className] of [
    [clear, 'Ausgewertet', 'status-badge ok'],
    [warning, 'Mit Hinweisen', 'status-badge warning'],
    [invalid, 'Handlungsbedarf', 'status-badge invalid'],
  ]) {
    const { context, element } = rendererContext();
    context.renderAll(payload);
    assert.equal(element('#status-badge').textContent, label);
    assert.equal(element('#status-badge').className, className);
  }
});

test('Druckbericht verwendet lesbaren Scope und wartet auf das Laden des Berichts', async () => {
  const { context } = rendererContext();
  const appended = [];
  const listeners = new Map();
  const revoked = [];
  const clearedTimers = [];
  let printCalls = 0;
  const popup = {
    document: { write() {} },
    location: { href: '' },
    addEventListener(name, callback) { listeners.set(name, callback); },
    focus() {},
    print() { printCalls += 1; },
    close() {},
  };

  context.FormData = class {
    append(name, value) { appended.push([name, value]); }
  };
  context.fetch = async () => ({ ok: true, blob: async () => ({ report: true }) });
  context.URL = {
    createObjectURL() { return 'blob:synthetic-report'; },
    revokeObjectURL(url) { revoked.push(url); },
  };
  context.window = { open: () => popup, alert() {} };
  context.setTimeout = () => 47;
  context.clearTimeout = (timer) => clearedTimers.push(timer);
  vm.runInContext("state.file = { name: 'synthetic.xml' };", context);

  await vm.runInContext('printReport()', context);
  assert.equal(popup.location.href, 'blob:synthetic-report');
  assert.equal(printCalls, 0);
  assert.equal(appended.find(([name]) => name === 'scope')?.[1], 'readable');

  listeners.get('load')();
  assert.equal(printCalls, 1);
  listeners.get('afterprint')();
  assert.deepEqual(clearedTimers, [47]);
  assert.deepEqual(revoked, ['blob:synthetic-report']);

  appended.length = 0;
  await vm.runInContext("fetchHtmlReport('complete')", context);
  assert.equal(appended.find(([name]) => name === 'scope')?.[1], 'complete');
});

test('Zahlungsfluss stellt neutrale, uneindeutige und abweichende Empfänger korrekt dar', () => {
  const noPayment = schemaTwoPayload();
  noPayment.roles.expected_payment_direction = 'none';
  noPayment.roles.expected_payer = null;
  noPayment.roles.expected_recipient = null;
  noPayment.payment.reference = null;
  noPayment.payment.instructions = [];
  noPayment.payment.terms = [];
  const noPaymentRenderer = rendererContext();
  noPaymentRenderer.context.renderAll(noPayment);
  const noPaymentHtml = noPaymentRenderer.element('#payment-section').innerHTML;
  assert.match(noPaymentHtml, /Keine Zahlung erwartet/);
  assert.match(noPaymentHtml, /Keine Zahlungsanweisungen angegeben/);
  assert.doesNotMatch(noPaymentHtml, /<h4/);
  assert.doesNotMatch(noPaymentHtml, /<h5/);
  assert.doesNotMatch(noPaymentHtml, /Zahlungsreferenz/);

  const ambiguous = schemaTwoPayload();
  ambiguous.roles.issuer = 'unknown';
  ambiguous.roles.document_recipient = 'seller';
  ambiguous.roles.expected_payment_direction = 'unknown';
  ambiguous.roles.expected_payer = null;
  ambiguous.roles.expected_recipient = null;
  ambiguous.roles.derivation = 'ambiguous';
  const ambiguousRenderer = rendererContext();
  ambiguousRenderer.context.renderAll(ambiguous);
  const ambiguousHtml = ambiguousRenderer.element('#payment-section').innerHTML;
  assert.equal((ambiguousHtml.match(/Nicht eindeutig ableitbar/g) || []).length, 2);
  assert.match(ambiguousHtml, /Wegen widersprüchlicher Angaben nicht eindeutig ableitbar/);

  const payee = schemaTwoPayload();
  payee.roles.expected_recipient = 'payee';
  const payeeRenderer = rendererContext();
  payeeRenderer.context.renderAll(payee);
  assert.match(payeeRenderer.element('#payment-section').innerHTML, /Käufer → Zahlungsempfänger/);
});

test('Zahlungsanweisungen, Zahlwege und Bedingungen bilden eine semantische Überschriftenhierarchie', () => {
  const payload = schemaTwoPayload();
  const instruction = payload.payment.instructions[0];
  instruction.credit_transfers = [{
    account_id: { value: 'DE02120300000000202051', scheme_id: 'IBAN' },
    account_name: 'Synthetischer Zahlungsempfänger',
    service_provider_id: { value: 'BYLADEM1001', scheme_id: 'BIC' },
  }, {
    account_id: { value: 'DE44500105175407324931', scheme_id: 'IBAN' },
    account_name: 'Synthetischer Zahlungsempfänger',
    service_provider_id: null,
  }];
  instruction.direct_debit = {
    mandate_reference: 'SYNTHETIC-MANDATE',
    creditor_id: { value: 'DE98ZZZ09999999999', scheme_id: 'SEPA' },
    debited_account_id: { value: 'DE44500105175407324931', scheme_id: 'IBAN' },
  };
  payload.payment.instructions.push({
    ...instruction,
    credit_transfers: [],
    payment_card: null,
    direct_debit: null,
  });
  payload.payment.terms = [{
    description: 'Zahlbar innerhalb von 14 Tagen.',
    due_date: '2026-08-14',
    partial_payment: null,
  }, {
    description: 'Synthetische zweite Zahlungsbedingung.',
    due_date: null,
    partial_payment: { value: '25.00', currency: 'EUR' },
  }];

  const { context, element } = rendererContext();
  context.renderAll(payload);
  const paymentHtml = element('#payment-section').innerHTML;

  assert.equal((paymentHtml.match(/class="payment-item"/g) || []).length, 2);
  assert.match(paymentHtml, /<h5 class="payment-heading payment-detail-heading">Überweisungskonto 1<\/h5>/);
  assert.match(paymentHtml, /<h5 class="payment-heading payment-detail-heading">Überweisungskonto 2<\/h5>/);
  assert.match(paymentHtml, /<h5 class="payment-heading payment-detail-heading">Zahlungskarte<\/h5>/);
  assert.match(paymentHtml, /<h5 class="payment-heading payment-detail-heading">Lastschrift<\/h5>/);
  assert.equal((paymentHtml.match(/class="payment-section payment-terms-section"/g) || []).length, 2);
  assert.match(paymentHtml, /<h3 class="payment-heading payment-section-heading">Zahlungsbedingung 1<\/h3>/);
  assert.match(paymentHtml, /<h3 class="payment-heading payment-section-heading">Zahlungsbedingung 2<\/h3>/);
});
