'use strict';

const state = {
  file: null,
  analysis: null,
  technicalRows: [],
  technicalPage: 1,
  technicalPageSize: 200,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function present(value) {
  return value !== null && value !== undefined && value !== '' && !(Array.isArray(value) && value.length === 0);
}

function text(value, fallback = '–') {
  return present(value) ? String(value) : fallback;
}

function taxCategoryDisplay(code, label, display) {
  if (present(display)) return String(display);
  if (present(code) && present(label)) {
    const codeText = String(code);
    const labelText = String(label);
    if (labelText === codeText || labelText.startsWith(`${codeText} –`)) return labelText;
    return `${codeText} – ${labelText}`;
  }
  return text(label || code, 'Steuer');
}

function taxRateDisplay(rate) {
  return present(rate) ? `${formatNumber(rate, 2)} %` : null;
}

function formatNumber(value, maxDigits = 4) {
  if (!present(value)) return '–';
  const number = Number(String(value).replace(',', '.'));
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat('de-DE', {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxDigits,
  }).format(number);
}

function formatMoney(value, currency = 'EUR') {
  if (!present(value)) return '–';
  const number = Number(String(value).replace(',', '.'));
  if (!Number.isFinite(number)) return `${value} ${currency || ''}`.trim();
  try {
    return new Intl.NumberFormat('de-DE', {
      style: 'currency',
      currency: currency || 'EUR',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(number);
  } catch (_error) {
    return `${number.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || ''}`.trim();
  }
}

function formatDate(value) {
  if (!present(value)) return '–';
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value);
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size)) return '–';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = size;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toLocaleString('de-DE', { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}

function safeFilename(value, fallback = 'e-rechnung') {
  const cleaned = String(value || fallback).replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return cleaned || fallback;
}

function detailRows(rows) {
  const visible = rows.filter((row) => present(row[1]));
  if (!visible.length) return '<p class="empty-state">Keine Angaben vorhanden.</p>';
  return `<dl class="detail-list">${visible.map(([label, value]) => `
    <div class="detail-row"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>
  `).join('')}</dl>`;
}

function addressLines(address = {}) {
  return [
    address.line1,
    address.line2,
    address.line3,
    [address.postcode, address.city].filter(present).join(' '),
    address.subdivision,
    address.country,
  ].filter(present);
}

function idList(entries = []) {
  return entries.filter((entry) => entry && present(entry.value)).map((entry) => `${entry.value}${entry.scheme ? ` (${entry.scheme})` : ''}`).join(', ');
}

function partyHasData(party = {}) {
  const address = party.address || {};
  const contact = party.contact || {};
  const endpoint = party.endpoint || {};
  return [
    party.name, party.trading_name, party.description, endpoint.value,
    ...(party.ids || []).map((entry) => entry?.value),
    ...(party.tax_ids || []).map((entry) => entry?.value),
    ...Object.values(address), ...Object.values(contact),
  ].some(present);
}

function renderParty(party = {}) {
  const address = party.address || {};
  const contact = party.contact || {};
  const endpoint = party.endpoint || {};
  const addressHtml = addressLines(address).length
    ? `<div class="address-block">${addressLines(address).map((line) => `<div>${escapeHtml(line)}</div>`).join('')}</div>`
    : '<p class="empty-state">Keine Anschrift angegeben.</p>';
  const details = detailRows([
    ['Handelsname', party.trading_name],
    ['Kennungen', idList(party.ids)],
    ['Steuerkennungen', idList(party.tax_ids)],
    ['Elektronische Adresse', endpoint.value ? `${endpoint.value}${endpoint.scheme ? ` (${endpoint.scheme})` : ''}` : null],
    ['Kontakt', contact.name],
    ['Abteilung', contact.department],
    ['Telefon', contact.phone],
    ['E-Mail', contact.email],
  ]);
  return `
    <div class="party-name">${escapeHtml(text(party.name, 'Nicht angegeben'))}</div>
    ${party.description ? `<p class="party-description">${escapeHtml(party.description)}</p>` : ''}
    ${addressHtml}
    ${details}
  `;
}

function renderSummary(data) {
  const doc = data.document || {};
  const totals = data.totals || {};
  const validation = data.validation || {};
  const counts = validation.counts || { error: 0, warning: 0, info: 0 };
  const statusMap = {
    ok: ['Unauffällig', 'ok'],
    warning: ['Auffällig', 'warning'],
    invalid: ['Fehlerhaft', 'invalid'],
  };
  const [statusLabel, statusClass] = statusMap[validation.status] || ['Unbekannt', 'warning'];
  const badge = $('#status-badge');
  badge.textContent = statusLabel;
  badge.className = `status-badge ${statusClass}`;
  $('#document-kind').textContent = doc.kind || 'E‑Rechnung';
  $('#document-title').textContent = `${doc.kind || 'E‑Rechnung'} ${doc.id || ''}`.trim();
  $('#document-subtitle').textContent = [doc.format, doc.profile_name, formatDate(doc.issue_date)].filter(present).join(' · ');
  $('#payable-total').textContent = formatMoney(totals.due_payable_amount, doc.currency);
  $('#due-date-summary').textContent = doc.due_date ? `Fällig am ${formatDate(doc.due_date)}` : 'Kein Fälligkeitsdatum angegeben';
  $('#summary-counts').innerHTML = [
    ['Fehler', counts.error || 0],
    ['Warnungen', counts.warning || 0],
    ['Hinweise', counts.info || 0],
  ].map(([label, count]) => `<div class="summary-count"><strong>${count}</strong><span>${label}</span></div>`).join('');
  const issueCount = (counts.error || 0) + (counts.warning || 0);
  $('#validation-tab-count').textContent = issueCount ? String(issueCount) : '✓';
}

function renderFacts(data) {
  const doc = data.document || {};
  const profile = data.profile || {};
  const facts = [
    ['Rechnungsnummer', doc.id],
    ['Rechnungsdatum', formatDate(doc.issue_date)],
    ['Liefer-/Leistungsdatum', formatDate(doc.delivery_date)],
    ['Fälligkeit', formatDate(doc.due_date)],
    ['Währung', doc.currency_label || doc.currency],
    ['Profil', profile.name || doc.profile_name],
    ['Rechnungsart', doc.type_label || doc.type_code],
    ['Käuferreferenz', doc.buyer_reference],
    ['Syntax', doc.syntax],
    ['Geschäftsprozess', profile.business_process_id],
    ['Steuerdatum', formatDate(doc.tax_point_date)],
    ['Profilkennung', profile.id || doc.profile_id],
  ];
  $('#document-facts').innerHTML = facts.map(([label, value]) => `
    <div class="fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text(value))}</strong></div>
  `).join('');
}

function renderLines(data) {
  const lines = data.lines || [];
  const currency = data.document?.currency || 'EUR';
  $('#line-count').textContent = `${lines.length} ${lines.length === 1 ? 'Position' : 'Positionen'}`;
  $('#line-items-body').innerHTML = lines.map((line, index) => {
    const itemIds = [
      line.seller_item_id ? `Art.-Nr. ${line.seller_item_id}` : null,
      line.buyer_item_id ? `Käufer-ID ${line.buyer_item_id}` : null,
      line.standard_item_id ? `${line.standard_item_id}${line.standard_item_scheme ? ` (${line.standard_item_scheme})` : ''}` : null,
    ].filter(present).join(' · ');

    const details = [...(line.notes || [])];
    if (line.period) {
      const period = [
        line.period.start ? `von ${formatDate(line.period.start)}` : null,
        line.period.end ? `bis ${formatDate(line.period.end)}` : null,
        line.period.description,
      ].filter(present).join(' ');
      if (period) details.push(`Abrechnungszeitraum: ${period}`);
    }
    if (line.order_line_reference) details.push(`Bestellposition: ${line.order_line_reference}`);
    if (line.accounting_cost) details.push(`Kontierung: ${line.accounting_cost}`);
    if (line.origin_country_label || line.origin_country) details.push(`Ursprungsland: ${line.origin_country_label || line.origin_country}`);
    (line.classifications || []).forEach((item) => {
      const value = [item.code, item.name, item.scheme ? `Schema ${item.scheme}` : null, item.version ? `Version ${item.version}` : null]
        .filter(present).join(' · ');
      if (value) details.push(`Klassifikation: ${value}`);
    });
    (line.additional_properties || []).forEach((item) => {
      if (present(item?.name) || present(item?.value)) details.push(`${item.name || 'Eigenschaft'}: ${text(item.value)}`);
    });
    (line.allowances_charges || []).forEach((item) => {
      const adjustment = [
        `${item.type_label}: ${formatMoney(item.amount, item.currency || currency)}`,
        present(item.percent) ? `${formatNumber(item.percent, 4)} %` : null,
        item.basis_amount ? `Basis ${formatMoney(item.basis_amount, item.basis_currency || currency)}` : null,
        item.reason || item.reason_code,
      ].filter(present).join(' · ');
      if (adjustment) details.push(adjustment);
    });

    const base = present(line.base_quantity) && Number(line.base_quantity) !== 1
      ? `${formatMoney(line.price, line.price_currency || currency)} je ${formatNumber(line.base_quantity)} ${line.base_unit_label || line.base_unit_code || ''}`
      : `${formatMoney(line.price, line.price_currency || currency)} je ${line.base_unit_label || line.base_unit_code || line.unit_label || line.unit_code || 'Einheit'}`;
    const tax = [taxCategoryDisplay(line.tax_category, line.tax_category_label, line.tax_category_display), taxRateDisplay(line.tax_rate)].filter(present).join(' · ');
    return `
      <tr>
        <td>${escapeHtml(text(line.id, index + 1))}</td>
        <td>
          <span class="line-name">${escapeHtml(text(line.name || line.description, 'Ohne Bezeichnung'))}</span>
          ${line.description && line.description !== line.name ? `<span class="line-note">${escapeHtml(line.description)}</span>` : ''}
          ${itemIds ? `<span class="line-meta">${escapeHtml(itemIds)}</span>` : ''}
          ${details.length ? `<span class="line-note">${escapeHtml(details.join(' · '))}</span>` : ''}
        </td>
        <td class="num">${escapeHtml(formatNumber(line.quantity))}<span class="line-meta">${escapeHtml(text(line.unit_label || line.unit_code, ''))}</span></td>
        <td class="num">${escapeHtml(base)}</td>
        <td class="num">${escapeHtml(text(tax))}</td>
        <td class="num"><strong>${escapeHtml(formatMoney(line.line_total, line.line_currency || currency))}</strong></td>
      </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty-state">Keine Rechnungspositionen erkannt.</td></tr>';
}

function renderTaxes(data) {
  const taxes = data.taxes || [];
  const currency = data.document?.currency || 'EUR';
  $('#tax-section').innerHTML = taxes.length ? taxes.map((tax) => {
    const details = [];
    if (present(tax.basis_amount)) {
      details.push(`${tax.basis_label || (tax.category_code === 'O' ? 'Nettobetrag dieser Steuerkategorie' : 'Bemessungsgrundlage')} ${formatMoney(tax.basis_amount, tax.basis_currency || currency)}`);
    }
    if (present(tax.exemption_reason)) details.push(`Begründung: ${tax.exemption_reason}`);
    if (present(tax.exemption_reason_code)) details.push(`Begründungscode: ${tax.exemption_reason_code}`);
    const heading = [
      taxCategoryDisplay(tax.category_code, tax.category_label, tax.category_display),
      taxRateDisplay(tax.rate),
    ].filter(present).join(' · ');
    return `
      <div class="tax-row">
        <div><span>${escapeHtml(heading)}</span>
        ${details.length ? `<small>${escapeHtml(details.join(' · '))}</small>` : ''}</div>
        <strong>${escapeHtml(formatMoney(tax.tax_amount, tax.tax_currency || currency))}</strong>
      </div>`;
  }).join('') : '<p class="empty-state">Keine Steueraufschlüsselung erkannt.</p>';
}

function renderTotals(data) {
  const totals = data.totals || {};
  const currency = data.document?.currency || totals.currency || 'EUR';
  const rows = [
    ['Summe Positionen', totals.line_total, ''],
    ['Nachlässe', totals.allowance_total, ''],
    ['Zuschläge', totals.charge_total, ''],
    ['Nettobetrag / Steuerbasis', totals.tax_basis_total, ''],
    ['Umsatzsteuer', totals.tax_total, ''],
    ['Bruttobetrag', totals.grand_total, 'grand'],
    ['Vorauszahlungen', totals.prepaid_amount, ''],
    ['Rundung', totals.rounding_amount, ''],
    ['Zahlbetrag', totals.due_payable_amount, 'payable'],
  ].filter((row) => present(row[1]));
  $('#totals-section').innerHTML = rows.map(([label, value, className]) => `
    <div class="total-row ${className}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatMoney(value, currency))}</strong></div>
  `).join('') || '<p class="empty-state">Keine Summen erkannt.</p>';
}

function renderAdditionalParties(data) {
  const roles = [
    ['Zahlungsempfänger', data.payee],
    ['Rechnungsempfänger', data.invoicee],
    ['Lieferempfänger', data.ship_to],
  ].filter(([, party]) => partyHasData(party || {}));
  const section = $('#additional-parties-section');
  section.hidden = roles.length === 0;
  section.innerHTML = roles.map(([label, party]) => `
    <article class="content-card"><h2>${escapeHtml(label)}</h2>${renderParty(party || {})}</article>
  `).join('');
}

function renderHeaderAdjustments(data) {
  const items = data.header_allowances_charges || [];
  const currency = data.document?.currency || data.totals?.currency || 'EUR';
  const card = $('#header-adjustments-card');
  card.hidden = items.length === 0;
  $('#header-adjustments-section').innerHTML = items.map((item, index) => {
    const detail = [
      present(item.percent) ? `${formatNumber(item.percent, 4)} %` : null,
      item.basis_amount ? `Basis ${formatMoney(item.basis_amount, item.basis_currency || currency)}` : null,
      item.reason,
      item.reason_code ? `Code ${item.reason_code}` : null,
    ].filter(present).join(' · ');
    return `<div class="tax-row"><div><span>${escapeHtml(item.type_label || `Anpassung ${index + 1}`)}</span><small>${escapeHtml(detail)}</small></div><strong>${escapeHtml(formatMoney(item.amount, item.currency || currency))}</strong></div>`;
  }).join('');
}

function renderPayment(data) {
  const payment = data.payment || {};
  const means = payment.means || [];
  const terms = payment.terms || [];
  const blocks = [];
  if (present(payment.reference)) {
    blocks.push(detailRows([['Zahlungsreferenz', payment.reference]]));
  }
  means.forEach((item, index) => {
    blocks.push(`<h3>Zahlungsweg ${index + 1}</h3>${detailRows([
      ['Art', item.type_label || item.type_code],
      ['Information', item.information],
      ['IBAN / Konto', item.iban],
      ['Kontoinhaber', item.account_name],
      ['BIC', item.bic],
      ['IBAN des Zahlers', item.payer_iban],
      ['Mandatsreferenz', item.mandate_reference],
      ['Gläubiger-ID', item.creditor_id],
      ['Kartennummer/Konto', item.card_account],
      ['Zahlungs-ID', item.payment_id],
    ])}`);
  });
  terms.forEach((item, index) => {
    blocks.push(`<h3>Zahlungsbedingung ${index + 1}</h3>${detailRows([
      ['Beschreibung', item.description],
      ['Fälligkeit', formatDate(item.due_date)],
      ['Lastschriftmandat', item.direct_debit_mandate_id],
      ['Teilzahlungsbetrag', item.partial_payment_amount],
    ])}`);
  });
  $('#payment-section').innerHTML = blocks.join('') || '<p class="empty-state">Keine Zahlungsangaben erkannt.</p>';
}

function renderReferences(data) {
  const refs = data.references || {};
  const delivery = data.delivery || {};
  const rows = [
    ['Bestellreferenz Käufer', refs.buyer_order],
    ['Bestellreferenz Verkäufer', refs.seller_order],
    ['Vertrag', refs.contract],
    ['Projekt', refs.project],
    ['Vorgängerrechnungen', (refs.preceding_invoices || []).join(', ')],
    ['Lieferdatum', formatDate(delivery.date)],
    ['Versandavis', delivery.despatch_advice_reference],
    ['Wareneingangsavis', delivery.receiving_advice_reference],
  ];
  const additional = (refs.additional_documents || []).filter((item) => Object.values(item || {}).some(present));
  let html = detailRows(rows);
  if (additional.length) {
    html += '<h3>Weitere Dokumente</h3>' + additional.map((item) => detailRows([
      ['ID', item.id], ['Typ', item.name || item.type_code], ['Beschreibung', item.description],
      ['Datei', item.attachment_filename], ['MIME', item.attachment_mime], ['URI', item.external_uri],
    ])).join('');
  }
  $('#references-section').innerHTML = html;
}

function renderNotes(data) {
  const notes = data.document?.notes || [];
  $('#notes-section').innerHTML = notes.length
    ? notes.map((note) => `<p class="note-box">${escapeHtml(note)}</p>`).join('')
    : '<p class="empty-state">Keine allgemeinen Rechnungshinweise enthalten.</p>';
}

function renderSource(data) {
  const source = data.source || {};
  const container = source.container || {};
  const attachments = source.attachments || [];
  let html = detailRows([
    ['Datei', source.filename],
    ['Dateityp', source.media_type],
    ['Größe', formatBytes(source.size)],
    ['Container', container.type],
    ['XML-Datei', source.xml_filename],
    ['XML-Größe', formatBytes(source.xml_size)],
    ['Seiten', container.page_count],
    ['SHA-256 Quelldatei', source.sha256],
    ['SHA-256 XML', source.xml_sha256],
    ['Verarbeitung', data.processing?.duration_ms ? `${formatNumber(data.processing.duration_ms, 2)} ms` : null],
  ]);
  if (attachments.length) {
    html += '<h3>Eingebettete Dateien</h3>' + attachments.map((item) => detailRows([
      ['Name', item.name], ['Größe', formatBytes(item.size)], ['XML', item.is_xml ? 'Ja' : 'Nein'], ['SHA-256', item.sha256],
    ])).join('');
  }
  $('#source-section').innerHTML = html;
}

function findingIcon(severity) {
  return severity === 'error' ? '×' : severity === 'warning' ? '!' : 'i';
}

function renderValidation(data) {
  const validation = data.validation || {};
  const builtin = validation.builtin || {};
  const official = validation.official || {};
  $('#validation-assessment').textContent = validation.assessment || 'Prüfergebnis';
  $('#builtin-scope').textContent = builtin.scope || '';
  let officialHtml;
  if (official.executed) {
    officialHtml = `<strong>${official.accepted ? 'KoSIT: akzeptiert' : 'KoSIT: abgelehnt'}</strong><span>${escapeHtml(text(official.summary))}</span>`;
  } else if (official.configured) {
    officialHtml = `<strong>KoSIT nicht ausgeführt</strong><span>${escapeHtml(text(official.summary))}</span>`;
  } else {
    const reason = present(official.summary)
      ? text(official.summary)
      : 'Für die vollständige XSD-/Schematron-Prüfung kann der offizielle Validator angebunden werden.';
    officialHtml = `<strong>KoSIT nicht eingerichtet</strong><span>${escapeHtml(reason)}</span>`;
  }
  $('#official-state').innerHTML = officialHtml;

  const findings = validation.findings || [];
  $('#findings-list').innerHTML = findings.map((item) => {
    const meta = [
      item.location ? `Ort: ${item.location}` : null,
      present(item.actual) ? `Ist: ${item.actual}` : null,
      present(item.expected) ? `Erwartet: ${item.expected}` : null,
      item.source ? `Quelle: ${item.source}` : null,
    ].filter(present);
    return `
      <article class="finding ${escapeHtml(item.severity || 'info')}">
        <span class="finding-icon" aria-hidden="true">${findingIcon(item.severity)}</span>
        <div><h3>${escapeHtml(text(item.title, 'Prüfmeldung'))}</h3><p>${escapeHtml(text(item.message, ''))}</p>
        ${meta.length ? `<div class="finding-meta">${meta.map((entry) => `<span>${escapeHtml(entry)}</span>`).join('')}</div>` : ''}</div>
        <span class="finding-code">${escapeHtml(text(item.id, '–'))}</span>
      </article>`;
  }).join('') || '<p class="empty-state">Keine Prüfmeldungen vorhanden.</p>';

  const details = $('#official-report-details');
  if (present(official.raw_report)) {
    details.hidden = false;
    $('#official-report-raw').textContent = official.raw_report;
  } else {
    details.hidden = true;
    $('#official-report-raw').textContent = '';
  }
}

function filteredTechnicalRows() {
  const query = ($('#technical-search').value || '').trim().toLowerCase();
  const rows = state.analysis?.technical?.rows || [];
  if (!query) return rows;
  return rows.filter((row) => [row.kind, row.path, row.name, row.namespace, row.value]
    .some((value) => String(value || '').toLowerCase().includes(query)));
}

function renderTechnicalPage(reset = false) {
  if (reset) state.technicalPage = 1;
  const rows = filteredTechnicalRows();
  state.technicalRows = rows;
  const totalPages = Math.max(1, Math.ceil(rows.length / state.technicalPageSize));
  state.technicalPage = Math.min(Math.max(1, state.technicalPage), totalPages);
  const start = (state.technicalPage - 1) * state.technicalPageSize;
  const pageRows = rows.slice(start, start + state.technicalPageSize);
  $('#technical-body').innerHTML = pageRows.map((row) => `
    <tr><td>${escapeHtml(text(row.kind))}</td><td title="${escapeHtml(text(row.namespace, ''))}">${escapeHtml(text(row.path))}</td><td>${escapeHtml(text(row.value, ''))}</td></tr>
  `).join('') || '<tr><td colspan="3" class="empty-state">Keine passenden XML-Daten gefunden.</td></tr>';
  $('#technical-page-info').textContent = `${rows.length.toLocaleString('de-DE')} Einträge · Seite ${state.technicalPage} von ${totalPages}`;
  $('#technical-prev').disabled = state.technicalPage <= 1;
  $('#technical-next').disabled = state.technicalPage >= totalPages;
}

function renderTechnical(data) {
  const technical = data.technical || {};
  $('#technical-summary').textContent = `${Number(technical.field_count || 0).toLocaleString('de-DE')} dargestellte Werte und Strukturangaben${technical.truncated ? ' (Darstellungsgrenze erreicht)' : ''}.`;
  $('#technical-search').value = '';
  renderTechnicalPage(true);
  $('#raw-xml').textContent = technical.raw_xml || '';
}

function renderAll(data) {
  state.analysis = data;
  renderSummary(data);
  renderFacts(data);
  $('#seller-card').innerHTML = renderParty(data.seller || {});
  $('#buyer-card').innerHTML = renderParty(data.buyer || {});
  renderAdditionalParties(data);
  renderHeaderAdjustments(data);
  renderLines(data);
  renderTaxes(data);
  renderTotals(data);
  renderPayment(data);
  renderReferences(data);
  renderNotes(data);
  renderSource(data);
  renderValidation(data);
  renderTechnical(data);
}

function showError(message) {
  const box = $('#error-box');
  box.textContent = message;
  box.hidden = false;
}

function setLoading(loading) {
  $('#progress').hidden = !loading;
  $('#drop-zone').setAttribute('aria-busy', loading ? 'true' : 'false');
  $('#file-input').disabled = loading;
  $$('.example-button').forEach((button) => { button.disabled = loading; });
}

async function parseError(response) {
  try {
    const payload = await response.json();
    if (typeof payload.detail === 'string') return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map((item) => item.msg || String(item)).join(' · ');
  } catch (_error) {
    // Fall back to status text.
  }
  return `Die Rechnung konnte nicht verarbeitet werden (${response.status} ${response.statusText}).`;
}

function officialValidationRequested() {
  const checkbox = $('#official-checkbox');
  return checkbox.checked && !checkbox.disabled;
}

async function analyzeFile(file) {
  if (!file) return;
  state.file = file;
  $('#error-box').hidden = true;
  setLoading(true);
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('official', officialValidationRequested() ? 'true' : 'false');
  try {
    const response = await fetch('/api/analyze', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await parseError(response));
    const data = await response.json();
    renderAll(data);
    $('#upload-view').hidden = true;
    $('#result-view').hidden = false;
    activateTab('invoice-panel');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) {
    showError(error instanceof Error ? error.message : String(error));
  } finally {
    setLoading(false);
  }
}

function activateTab(panelId) {
  $$('.tab-button').forEach((button) => button.classList.toggle('active', button.dataset.tab === panelId));
  $$('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === panelId));
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadJson() {
  if (!state.analysis) return;
  const id = state.analysis.document?.id || 'bericht';
  const blob = new Blob([JSON.stringify(state.analysis, null, 2)], { type: 'application/json;charset=utf-8' });
  downloadBlob(blob, `${safeFilename(id)}-pruefbericht.json`);
}

async function downloadXml() {
  if (!state.analysis || !state.file) return;
  try {
    const form = new FormData();
    form.append('file', state.file, state.file.name);
    const response = await fetch('/api/xml', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await parseError(response));
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || state.analysis.source?.xml_filename || 'rechnung.xml';
    downloadBlob(blob, safeFilename(filename, 'rechnung.xml'));
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function fetchHtmlReport() {
  if (!state.file) throw new Error('Keine Rechnung geladen.');
  const form = new FormData();
  form.append('file', state.file, state.file.name);
  form.append('official', officialValidationRequested() ? 'true' : 'false');
  const response = await fetch('/api/report', { method: 'POST', body: form });
  if (!response.ok) throw new Error(await parseError(response));
  return response.blob();
}

async function downloadHtml() {
  try {
    const blob = await fetchHtmlReport();
    const id = state.analysis?.document?.id || 'bericht';
    downloadBlob(blob, `${safeFilename(id)}-lesbare-e-rechnung.html`);
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function printReport() {
  const printWindow = window.open('', '_blank');
  if (!printWindow) {
    window.alert('Das Druckfenster wurde vom Browser blockiert. Bitte Pop-ups für diese lokale Anwendung erlauben.');
    return;
  }
  printWindow.document.write('<!doctype html><title>Bericht wird erstellt</title><p style="font-family:system-ui;padding:2rem">Bericht wird erstellt …</p>');
  try {
    const blob = await fetchHtmlReport();
    const url = URL.createObjectURL(blob);
    printWindow.location.href = url;
    setTimeout(() => {
      try { printWindow.focus(); printWindow.print(); } catch (_error) { /* The report remains open for manual printing. */ }
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    }, 1200);
  } catch (error) {
    printWindow.close();
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function copyXml() {
  const xml = state.analysis?.technical?.raw_xml || '';
  try {
    await navigator.clipboard.writeText(xml);
    const button = $('#copy-xml-button');
    const old = button.textContent;
    button.textContent = 'Kopiert ✓';
    setTimeout(() => { button.textContent = old; }, 1400);
  } catch (_error) {
    window.alert('Das XML konnte nicht in die Zwischenablage kopiert werden.');
  }
}

async function loadExample(name) {
  $('#error-box').hidden = true;
  setLoading(true);
  try {
    const response = await fetch(`/api/examples/${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error('Das Beispiel konnte nicht geladen werden.');
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match ? match[1] : `${name}-beispiel.xml`;
    const file = new File([blob], filename, { type: 'application/xml' });
    await analyzeFile(file);
  } catch (error) {
    showError(error instanceof Error ? error.message : String(error));
    setLoading(false);
  }
}

function resetView() {
  state.file = null;
  state.analysis = null;
  $('#file-input').value = '';
  $('#result-view').hidden = true;
  $('#upload-view').hidden = false;
  $('#error-box').hidden = true;
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function initialise() {
  const dropZone = $('#drop-zone');
  const fileInput = $('#file-input');

  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', () => analyzeFile(fileInput.files?.[0]));

  ['dragenter', 'dragover'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add('dragging');
  }));
  ['dragleave', 'drop'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragging');
  }));
  dropZone.addEventListener('drop', (event) => analyzeFile(event.dataTransfer?.files?.[0]));

  $$('.example-button').forEach((button) => button.addEventListener('click', () => loadExample(button.dataset.example)));
  $$('.tab-button').forEach((button) => button.addEventListener('click', () => activateTab(button.dataset.tab)));

  $('#new-file-button').addEventListener('click', resetView);
  $('#download-json-button').addEventListener('click', downloadJson);
  $('#download-xml-button').addEventListener('click', downloadXml);
  $('#download-html-button').addEventListener('click', downloadHtml);
  $('#print-button').addEventListener('click', printReport);
  $('#copy-xml-button').addEventListener('click', copyXml);

  $('#technical-search').addEventListener('input', () => renderTechnicalPage(true));
  $('#technical-prev').addEventListener('click', () => { state.technicalPage -= 1; renderTechnicalPage(); });
  $('#technical-next').addEventListener('click', () => { state.technicalPage += 1; renderTechnicalPage(); });
}

document.addEventListener('DOMContentLoaded', initialise);
