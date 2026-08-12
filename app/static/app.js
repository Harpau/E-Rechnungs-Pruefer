'use strict';

const UI_REVISION_HEADER = 'X-Einvoice-UI-Revision';
const uiRevision = document.currentScript?.dataset.uiRevision || '';
const REQUIRED_UI_IDS = Object.freeze([
  'additional-parties-section',
  'builtin-scope',
  'buyer-card',
  'copy-xml-button',
  'document-facts',
  'document-subtitle',
  'document-title',
  'document-type-summary',
  'download-complete-html-button',
  'download-html-button',
  'download-json-button',
  'download-xml-button',
  'drop-zone',
  'due-date-summary',
  'error-box',
  'file-input',
  'findings-list',
  'header-adjustments-card',
  'header-adjustments-section',
  'line-count',
  'line-items-body',
  'line-tax-breakdown-notices',
  'new-file-button',
  'notes-section',
  'official-checkbox',
  'official-report-details',
  'official-report-raw',
  'official-state',
  'payable-total',
  'payment-section',
  'print-button',
  'progress',
  'raw-xml',
  'references-section',
  'result-view',
  'seller-card',
  'source-section',
  'status-badge',
  'summary-counts',
  'tax-section',
  'technical-body',
  'technical-next',
  'technical-page-info',
  'technical-prev',
  'technical-search',
  'technical-summary',
  'totals-section',
  'upload-view',
  'validation-assessment',
  'validation-tab-count',
]);

const state = {
  file: null,
  analysis: null,
  technicalRows: [],
  technicalPage: 1,
  technicalPageSize: 200,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function uiFetch(input, options = {}) {
  return fetch(input, {
    ...options,
    headers: {
      ...(options.headers || {}),
      [UI_REVISION_HEADER]: uiRevision,
    },
  });
}

function showUiCompatibilityError(message) {
  const alert = document.createElement('main');
  alert.setAttribute('role', 'alert');
  alert.className = 'compatibility-error';
  alert.textContent = message;
  document.body.replaceChildren(alert);
}

function uiContractIsUsable() {
  const revisionPattern = /^[0-9a-f]{64}$/;
  if (!revisionPattern.test(uiRevision) || document.body?.dataset.uiRevision !== uiRevision) {
    showUiCompatibilityError(
      'Die geöffnete Oberfläche gehört zu einer anderen Anwendungsversion. Bitte schließen Sie dieses Fenster und öffnen Sie den E-Rechnungs-Prüfer erneut.',
    );
    return false;
  }

  const missingIds = REQUIRED_UI_IDS.filter((id) => document.getElementById(id) === null);
  if (missingIds.length > 0 || document.querySelector('.upload-card') === null) {
    showUiCompatibilityError(
      'Die Oberfläche wurde nicht vollständig geladen. Bitte schließen Sie dieses Fenster und öffnen Sie den E-Rechnungs-Prüfer erneut.',
    );
    return false;
  }
  return true;
}

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

function formatNumber(value, maxDigits = 4) {
  if (!present(value)) return '–';
  const number = Number(String(value).replace(',', '.'));
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat('de-DE', {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxDigits,
  }).format(number);
}

function codeDisplay(code, fallback = '–') {
  if (!code || !present(code.value)) return fallback;
  const value = String(code.value);
  if (!present(code.label)) return value;
  const label = String(code.label);
  if (label === value || label.startsWith(`${value} –`)) return label;
  return `${value} – ${label}`;
}

function identifierDisplay(identifier, fallback = '–') {
  if (!identifier || !present(identifier.value)) return fallback;
  return `${identifier.value}${present(identifier.scheme_id) ? ` (${identifier.scheme_id})` : ''}`;
}

function formatMoney(amount) {
  if (!amount || !present(amount.value)) return '–';
  const number = Number(String(amount.value).replace(',', '.'));
  const currency = present(amount.currency) ? String(amount.currency) : null;
  if (!Number.isFinite(number)) return `${amount.value} ${currency || ''}`.trim();
  if (!currency) {
    return number.toLocaleString('de-DE', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  try {
    return new Intl.NumberFormat('de-DE', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(number);
  } catch (_error) {
    return `${number.toLocaleString('de-DE', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })} ${currency}`.trim();
  }
}

function formatDate(value) {
  if (!present(value)) return '–';
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value);
}

function formatPeriod(period) {
  if (!period) return null;
  const parts = [
    period.start_date ? `von ${formatDate(period.start_date)}` : null,
    period.end_date ? `bis ${formatDate(period.end_date)}` : null,
    period.description,
  ].filter(present);
  return parts.length ? parts.join(' ') : null;
}

function formatQuantity(quantity) {
  if (!quantity || !present(quantity.value)) return '–';
  const unit = codeDisplay(quantity.unit, null);
  return [formatNumber(quantity.value), unit].filter(present).join(' ');
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

function subsectionHeading(label) {
  return `<h3 class="subsection-heading">${escapeHtml(label)}</h3>`;
}

function addressLines(address = null) {
  if (!address) return [];
  return [
    address.line1,
    address.line2,
    address.line3,
    [address.postcode, address.city].filter(present).join(' '),
    address.subdivision,
    codeDisplay(address.country, null),
  ].filter(present);
}

const PARTY_IDENTIFIER_KIND_LABELS = {
  party: 'Parteikennung',
  'legal-registration': 'Registerkennung',
  vat: 'USt-Kennung',
  'tax-registration': 'Steuerkennung',
  other: 'Weitere Kennung',
};

function partyIdentifierList(entries = []) {
  return entries
    .filter((entry) => entry?.identifier && present(entry.identifier.value))
    .map((entry) => {
      const kind = PARTY_IDENTIFIER_KIND_LABELS[entry.kind] || 'Kennung';
      return `${kind}: ${identifierDisplay(entry.identifier)}`;
    })
    .join(', ');
}

function partyHasData(party = null) {
  if (!party) return false;
  const address = party.postal_address || {};
  const contact = party.contact || {};
  return [
    party.legal_name,
    party.trading_name,
    party.additional_legal_information,
    party.electronic_address?.value,
    ...(party.identifiers || []).map((entry) => entry?.identifier?.value),
    ...(party.tax_identifiers || []).map((entry) => entry?.identifier?.value),
    address.line1,
    address.line2,
    address.line3,
    address.postcode,
    address.city,
    address.subdivision,
    address.country?.value,
    contact.name,
    contact.department,
    contact.phone,
    contact.email,
  ].some(present);
}

function renderParty(party = null) {
  if (!party) return '<p class="empty-state">Keine Angaben vorhanden.</p>';
  const address = party.postal_address;
  const contact = party.contact || {};
  const addressHtml = addressLines(address).length
    ? `<div class="address-block">${addressLines(address).map((line) => `<div>${escapeHtml(line)}</div>`).join('')}</div>`
    : '<p class="empty-state">Keine Anschrift angegeben.</p>';
  const details = detailRows([
    ['Handelsname', party.trading_name],
    ['Kennungen', partyIdentifierList(party.identifiers)],
    ['Steuerkennungen', partyIdentifierList(party.tax_identifiers)],
    ['Elektronische Adresse', identifierDisplay(party.electronic_address, null)],
    ['Kontakt', contact.name],
    ['Abteilung', contact.department],
    ['Telefon', contact.phone],
    ['E-Mail', contact.email],
  ]);
  return `
    <div class="party-name">${escapeHtml(text(party.legal_name, 'Nicht angegeben'))}</div>
    ${party.additional_legal_information ? `<p class="party-description">${escapeHtml(party.additional_legal_information)}</p>` : ''}
    ${addressHtml}
    ${details}
  `;
}

const DOCUMENT_FAMILY_LABELS = {
  invoice: 'Rechnung',
  'credit-note': 'Gutschrift',
  correction: 'Korrekturrechnung',
  'debit-note': 'Belastungsanzeige',
  'prepayment-invoice': 'Vorauszahlungsrechnung',
  'payment-request': 'Zahlungsaufforderung',
  'pro-forma': 'Pro-forma-Rechnung',
  information: 'Informationsdokument',
  claim: 'Forderungsdokument',
  other: 'Sonstiges Rechnungsdokument',
  unknown: 'E‑Rechnung',
};

const ROLE_LABELS = {
  seller: 'Verkäufer',
  buyer: 'Käufer',
  payee: 'Zahlungsempfänger',
  'invoice-recipient': 'Rechnungsempfänger',
  'delivery-recipient': 'Lieferempfänger',
  'seller-tax-representative': 'Steuervertreter des Verkäufers',
  unknown: 'Unbekannt',
};

const DOCUMENT_TYPE_STATUS_LABELS = {
  known: 'Erkannt',
  unknown: 'Unbekannter Code',
  missing: 'Nicht angegeben',
};

const POLARITY_LABELS = {
  debit: 'Soll',
  credit: 'Haben',
  neutral: 'Neutral',
  undetermined: 'Nicht bestimmbar',
};

const SETTLEMENT_RELEVANCE_LABELS = {
  relevant: 'Zahlungsrelevant',
  'not-relevant': 'Nicht zahlungsrelevant',
  undetermined: 'Nicht bestimmbar',
};

const ROOT_COMPATIBILITY_LABELS = {
  compatible: 'Kompatibel',
  incompatible: 'Nicht kompatibel',
  'not-applicable': 'Nicht anwendbar',
  undetermined: 'Nicht bestimmbar',
};

const RECOGNITION_CAPABILITY_LABELS = {
  recognized: 'Erkannt',
  unknown: 'Unbekannt',
  missing: 'Fehlend',
};

const RENDERING_CAPABILITY_LABELS = {
  full: 'Vollständig',
  partial: 'Teilweise',
  unsupported: 'Nicht unterstützt',
};

const INTERNAL_CHECKS_CAPABILITY_LABELS = {
  full: 'Vollständig',
  partial: 'Teilweise',
  unsupported: 'Nicht unterstützt',
};

const OFFICIAL_VALIDATION_CAPABILITY_LABELS = {
  bundled: 'Enthalten',
  'not-bundled': 'Nicht enthalten',
  unknown: 'Unbekannt',
  unavailable: 'Nicht verfügbar',
};

function documentKind(type = {}) {
  return DOCUMENT_FAMILY_LABELS[type.family] || DOCUMENT_FAMILY_LABELS.unknown;
}

function documentTypeSummary(type = {}) {
  if (type.status === 'missing' || !type.code || !present(type.code.value)) {
    return 'Rechnungsart · Nicht angegeben';
  }
  if (type.status === 'unknown') {
    return `Rechnungsart · ${type.code.value} – Unbekannter Dokumenttyp`;
  }
  return `Rechnungsart · ${codeDisplay(type.code, documentKind(type))}`;
}

const OFFICIAL_STATUS_LABELS = {
  accepted: 'Akzeptiert',
  rejected: 'Abgelehnt',
  'not-requested': 'Nicht angefordert',
  unsupported: 'Nicht unterstützt',
  unavailable: 'Nicht verfügbar',
  indeterminate: 'Unbestimmt',
};

const INTERNAL_STATUS_LABELS = {
  clear: 'Unauffällig',
  attention: 'Hinweise',
  errors: 'Fehler',
  'not-run': 'Nicht ausgeführt',
};

const PROCESSING_STATUS_LABELS = {
  complete: 'Vollständig',
  limited: 'Begrenzt',
  incomplete: 'Unvollständig',
};

function axisFindingCount(axis = {}) {
  const counts = axis.counts || {};
  return Number(counts.error || 0) + Number(counts.warning || 0) + Number(counts.info || 0);
}

function renderSummary(data) {
  const doc = data.document || {};
  const totals = data.totals || {};
  const profile = data.profile || {};
  const capabilities = data.capabilities || {};
  const assessment = data.assessment || {};
  const official = assessment.official || {};
  const internal = assessment.internal || {};
  const processing = assessment.processing || {};
  const hasErrors = official.status === 'rejected'
    || internal.status === 'errors'
    || processing.status === 'incomplete';
  const hasWarnings = internal.status === 'attention'
    || internal.status === 'not-run'
    || processing.status === 'limited'
    || ['unsupported', 'unavailable', 'indeterminate'].includes(official.status);
  const statusLabel = hasErrors ? 'Handlungsbedarf' : hasWarnings ? 'Mit Hinweisen' : 'Ausgewertet';
  const statusClass = hasErrors ? 'invalid' : hasWarnings ? 'warning' : 'ok';
  const badge = $('#status-badge');
  badge.textContent = statusLabel;
  badge.className = `status-badge ${statusClass}`;
  const kind = documentKind(doc.type);
  $('#document-type-summary').textContent = documentTypeSummary(doc.type);
  $('#document-title').textContent = `${kind} ${doc.id || ''}`.trim();
  $('#document-subtitle').textContent = [
    capabilities.format_name,
    profile.name,
    doc.issue_date ? formatDate(doc.issue_date) : null,
  ].filter(present).join(' · ');
  const payableLabel = $('#payable-total').previousElementSibling;
  if (payableLabel) payableLabel.textContent = 'Ausstehender Betrag (BT-115)';
  $('#payable-total').textContent = formatMoney(totals.payable);
  $('#due-date-summary').textContent = data.payment?.due_date
    ? `Fällig am ${formatDate(data.payment.due_date)}`
    : 'Kein Fälligkeitsdatum angegeben';
  $('#summary-counts').innerHTML = [
    ['Offiziell', OFFICIAL_STATUS_LABELS[official.status] || 'Unbekannt'],
    ['Intern', INTERNAL_STATUS_LABELS[internal.status] || 'Unbekannt'],
    ['Verarbeitung', PROCESSING_STATUS_LABELS[processing.status] || 'Unbekannt'],
  ].map(([label, value]) => `
    <div class="summary-count"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>
  `).join('');
  const issueCount = axisFindingCount(official) + axisFindingCount(internal) + axisFindingCount(processing);
  $('#validation-tab-count').textContent = issueCount ? String(issueCount) : '✓';
}

function renderFacts(data) {
  const doc = data.document || {};
  const type = doc.type || {};
  const profile = data.profile || {};
  const capabilities = data.capabilities || {};
  const periods = data.periods || {};
  const delivery = data.delivery || {};
  const deliveryLocation = delivery.location || {};
  const facts = [
    ['Rechnungsnummer', doc.id],
    ['Rechnungsdatum', formatDate(doc.issue_date)],
    ['Rechnungszeitraum', formatPeriod(periods.invoice)],
    ['Liefer-/Leistungszeitraum', formatPeriod(periods.delivery)],
    ['Tatsächliches Lieferdatum (BT-72)', formatDate(delivery.actual_date)],
    ['Kennung des Lieferorts (BT-71)', identifierDisplay(deliveryLocation.id, null)],
    ['Lieferort', addressLines(deliveryLocation.postal_address).join(', ')],
    ['Fälligkeit', formatDate(data.payment?.due_date)],
    ['Rechnungswährung', codeDisplay(doc.document_currency, null)],
    ['USt-Abrechnungswährung', codeDisplay(doc.vat_accounting_currency, null)],
    ['Profil', profile.name],
    ['Rechnungsart', codeDisplay(type.code, documentKind(type))],
    ['Dokumenttyp-Status', DOCUMENT_TYPE_STATUS_LABELS[type.status]],
    ['Dokumentfamilie', DOCUMENT_FAMILY_LABELS[type.family]],
    ['Grundpolarität', POLARITY_LABELS[type.base_polarity]],
    ['Abrechnungsrelevanz', SETTLEMENT_RELEVANCE_LABELS[type.settlement_relevance]],
    ['Selbstausstellung', type.self_billing === null || type.self_billing === undefined ? null : type.self_billing ? 'Ja' : 'Nein'],
    ['Käuferreferenz', doc.buyer_reference],
    ['Syntax', [capabilities.syntax, capabilities.syntax_version].filter(present).join(' ')],
    ['Format', capabilities.format_name],
    ['Dokumenttyperkennung', RECOGNITION_CAPABILITY_LABELS[capabilities.document_type_recognition]],
    ['Darstellungsumfang', RENDERING_CAPABILITY_LABELS[capabilities.rendering]],
    ['Interner Prüfumfang', INTERNAL_CHECKS_CAPABILITY_LABELS[capabilities.internal_checks]],
    ['Offizielle Prüfung', OFFICIAL_VALIDATION_CAPABILITY_LABELS[capabilities.official_validation]],
    ['Geschäftsprozess', profile.business_process_id],
    ['Steuerdatum', formatDate(doc.tax_point_date)],
    ['Code des Steuerdatums', codeDisplay(doc.tax_point_date_code, null)],
    ['Profilkennung', profile.id],
    ['UBL-Wurzelelement', type.ubl_root],
    ['Wurzel/Typ-Kompatibilität', ROOT_COMPATIBILITY_LABELS[type.root_compatibility]],
  ];
  $('#document-facts').innerHTML = facts.map(([label, value]) => `
    <div class="fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text(value))}</strong></div>
  `).join('');
}

function allowanceChargeKindLabel(kind, index) {
  if (kind === 'allowance') return 'Nachlass';
  if (kind === 'charge') return 'Zuschlag';
  return `Anpassung ${index + 1} (Art unbekannt)`;
}

function allowanceChargeDetail(item, index) {
  return [
    `${allowanceChargeKindLabel(item.kind, index)}: ${formatMoney(item.amount)}`,
    present(item.percentage) ? `${formatNumber(item.percentage, 4)} %` : null,
    item.base_amount ? `Basis ${formatMoney(item.base_amount)}` : null,
    item.reason_text,
    item.reason_code ? `Code ${codeDisplay(item.reason_code)}` : null,
    item.tax_category ? `Steuer ${codeDisplay(item.tax_category)}` : null,
    present(item.tax_rate_percent) ? `${formatDecimalExact(item.tax_rate_percent)} %` : null,
    present(item.indicator_raw) ? `Indikator ${item.indicator_raw}` : null,
  ].filter(present).join(' · ');
}

function normalizedDecimal(value) {
  if (!present(value)) return null;
  const raw = String(value).trim().replace(',', '.');
  const match = raw.match(/^([+-]?)(\d+)(?:\.(\d*))?$/);
  if (!match) return `raw:${raw}`;
  const integer = match[2].replace(/^0+(?=\d)/, '');
  const fraction = (match[3] || '').replace(/0+$/, '');
  const isZero = integer === '0' && !fraction;
  const sign = match[1] === '-' && !isZero ? '-' : '';
  return `${sign}${integer}${fraction ? `.${fraction}` : ''}`;
}

function formatDecimalExact(value) {
  if (!present(value)) return '–';
  const normalized = normalizedDecimal(value);
  if (!normalized || normalized.startsWith('raw:')) return String(value).trim();
  const sign = normalized.startsWith('-') ? '-' : '';
  const unsigned = sign ? normalized.slice(1) : normalized;
  const [integer, fraction] = unsigned.split('.');
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  return `${sign}${grouped}${fraction ? `,${fraction}` : ''}`;
}

function taxCategoryValue(category) {
  return category && present(category.value) ? String(category.value).trim() : null;
}

function taxCategoryLabel(category) {
  const value = taxCategoryValue(category);
  if (!value || !present(category?.label)) return null;
  const label = String(category.label).trim();
  const valueFolded = value.toUpperCase();
  if (label.toUpperCase() === valueFolded) return null;
  const hasCodePrefix = label.slice(0, value.length).toUpperCase() === valueFolded;
  if (hasCodePrefix && label.slice(value.length).startsWith(' – ')) return label.slice(value.length + 3);
  if (hasCodePrefix && label.slice(value.length).startsWith(' - ')) return label.slice(value.length + 3);
  return label;
}

function taxCategoryDisplay(category, fallback = '–') {
  const value = taxCategoryValue(category);
  if (!value) return fallback;
  const label = taxCategoryLabel(category);
  return label ? `${value} – ${label}` : value;
}

function taxCombinationKey(category, rate) {
  const categoryValue = taxCategoryValue(category);
  const normalizedRate = normalizedDecimal(rate);
  if (!categoryValue && normalizedRate === null) return null;
  return `${categoryValue?.toUpperCase() ?? '<missing>'}\u0000${normalizedRate ?? '<missing>'}`;
}

function lineTaxAccessibleText(line) {
  const categoryValue = taxCategoryValue(line.tax_category);
  const categoryLabel = taxCategoryLabel(line.tax_category);
  const hasRate = present(line.tax_rate_percent);
  const parts = [];
  if (hasRate) {
    parts.push(`Steuersatz ${formatDecimalExact(line.tax_rate_percent)} Prozent`);
  }
  if (categoryValue) {
    parts.push(`Steuerkategorie ${categoryValue}`);
    if (categoryLabel) parts.push(categoryLabel);
  }
  if (!hasRate && categoryValue) {
    parts.push(categoryValue.toUpperCase() === 'O' ? 'ohne Steuersatz' : 'Steuersatz nicht angegeben');
  } else if (hasRate && !categoryValue) {
    parts.push('Steuerkategorie nicht angegeben');
  } else if (!hasRate && !categoryValue) {
    parts.push('Steuersatz und Steuerkategorie nicht angegeben');
  }
  if (line.tax_type && present(line.tax_type.value)) {
    parts.push(`Steuerart ${codeDisplay(line.tax_type)}`);
  }
  return parts.join(', ');
}

function renderLineTaxCell(line) {
  const categoryValue = taxCategoryValue(line.tax_category);
  const hasRate = present(line.tax_rate_percent);
  let visible;
  if (hasRate) {
    const secondary = categoryValue
      ? `<span class="line-tax-code">${escapeHtml(categoryValue)}</span>`
      : '<span class="line-tax-status">Kategorie nicht angegeben</span>';
    visible = `<strong class="line-tax-primary line-tax-rate">${escapeHtml(formatDecimalExact(line.tax_rate_percent))}&nbsp;%</strong>${secondary}`;
  } else if (categoryValue) {
    const status = categoryValue.toUpperCase() === 'O'
      ? 'ohne Steuersatz'
      : 'Steuersatz nicht angegeben';
    visible = `<strong class="line-tax-primary line-tax-primary-code">${escapeHtml(categoryValue)}</strong><span class="line-tax-status">${status}</span>`;
  } else {
    visible = '<span class="line-tax-empty">–</span>';
  }
  return `<span class="line-tax-visual" aria-hidden="true">${visible}</span><span class="visually-hidden">${escapeHtml(lineTaxAccessibleText(line))}</span>`;
}

function renderLineTaxBreakdownNotices(data, lines) {
  const notice = $('#line-tax-breakdown-notices');
  if (!notice) return;
  const breakdownKeys = new Set((data.tax?.breakdown || [])
    .map((tax) => taxCombinationKey(tax.category, tax.rate_percent))
    .filter(present));
  const missing = new Map();
  lines.forEach((line) => {
    const key = taxCombinationKey(line.tax_category, line.tax_rate_percent);
    if (key && !breakdownKeys.has(key) && !missing.has(key)) missing.set(key, line);
  });
  if (!missing.size) {
    notice.innerHTML = '';
    notice.hidden = true;
    return;
  }
  const items = [...missing.values()].map((line) => {
    const category = taxCategoryDisplay(line.tax_category, 'Kategorie nicht angegeben');
    const categoryValue = taxCategoryValue(line.tax_category);
    let rate;
    if (present(line.tax_rate_percent)) {
      rate = `${escapeHtml(formatDecimalExact(line.tax_rate_percent))}&nbsp;%`;
    } else {
      rate = categoryValue?.toUpperCase() === 'O' ? 'ohne Steuersatz' : 'Steuersatz nicht angegeben';
    }
    return `<li>${escapeHtml(category)} · ${rate}</li>`;
  }).join('');
  notice.innerHTML = `<strong>Nicht in der Steueraufschlüsselung enthalten:</strong> <ul>${items}</ul>`;
  notice.hidden = false;
}

function renderLines(data) {
  const lines = data.lines || [];
  $('#line-count').textContent = `${lines.length} ${lines.length === 1 ? 'Position' : 'Positionen'}`;
  $('#line-items-body').innerHTML = lines.map((line, index) => {
    const item = line.item || {};
    const price = line.price || {};
    const itemIds = [
      item.seller_identifier ? `Art.-Nr. ${identifierDisplay(item.seller_identifier)}` : null,
      item.buyer_identifier ? `Käufer-ID ${identifierDisplay(item.buyer_identifier)}` : null,
      item.standard_identifier ? `Standard-ID ${identifierDisplay(item.standard_identifier)}` : null,
    ].filter(present).join(' · ');

    const details = [...(line.notes || [])];
    if (line.period) {
      const period = formatPeriod(line.period);
      if (period) details.push(`Abrechnungszeitraum: ${period}`);
    }
    if (line.order_line_reference) details.push(`Bestellposition: ${line.order_line_reference}`);
    if (line.accounting_reference) details.push(`Kontierung: ${line.accounting_reference}`);
    if (line.object_identifier) details.push(`Objektkennung: ${identifierDisplay(line.object_identifier)}`);
    if (item.origin_country) details.push(`Ursprungsland: ${codeDisplay(item.origin_country)}`);
    (item.classifications || []).forEach((classification) => {
      const value = [
        classification.code,
        classification.name,
        classification.scheme_id ? `Schema ${classification.scheme_id}` : null,
        classification.scheme_version ? `Version ${classification.scheme_version}` : null,
      ]
        .filter(present).join(' · ');
      if (value) details.push(`Klassifikation: ${value}`);
    });
    (item.properties || []).forEach((property) => {
      if (present(property?.name) || present(property?.value)) {
        details.push(`${property.name || 'Eigenschaft'}: ${text(property.value)}`);
      }
    });
    (line.allowances_charges || []).forEach((adjustment, adjustmentIndex) => {
      const adjustmentText = allowanceChargeDetail(adjustment, adjustmentIndex);
      if (adjustmentText) details.push(adjustmentText);
    });
    if (price.gross) details.push(`Bruttopreis: ${formatMoney(price.gross)}`);
    if (price.discount) {
      const discount = [
        price.discount.amount ? `Preisnachlass: ${formatMoney(price.discount.amount)}` : 'Preisnachlass',
        present(price.discount.percentage) ? `${formatNumber(price.discount.percentage, 4)} %` : null,
      ].filter(present).join(' · ');
      if (discount) details.push(discount);
    }

    const base = price.net
      ? [
        formatMoney(price.net),
        price.base_quantity ? `je ${formatQuantity(price.base_quantity)}` : null,
      ].filter(present).join(' ')
      : '–';
    return `
      <tr>
        <td>${escapeHtml(text(line.id, index + 1))}</td>
        <td>
          <span class="line-name">${escapeHtml(text(item.name || item.description, 'Ohne Bezeichnung'))}</span>
          ${item.description && item.description !== item.name ? `<span class="line-note">${escapeHtml(item.description)}</span>` : ''}
          ${itemIds ? `<span class="line-meta">${escapeHtml(itemIds)}</span>` : ''}
          ${details.length ? `<span class="line-note">${escapeHtml(details.join(' · '))}</span>` : ''}
        </td>
        <td class="num">${escapeHtml(formatQuantity(line.quantity))}</td>
        <td class="num">${escapeHtml(base)}</td>
        <td class="num line-tax-cell">${renderLineTaxCell(line)}</td>
        <td class="num"><strong>${escapeHtml(formatMoney(line.net_amount))}</strong></td>
      </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty-state">Keine Rechnungspositionen erkannt.</td></tr>';
  renderLineTaxBreakdownNotices(data, lines);
}

function renderTaxes(data) {
  const taxModel = data.tax || {};
  const breakdown = taxModel.breakdown || [];
  const taxTotals = taxModel.totals || {};
  let html = breakdown.length ? breakdown.map((tax) => {
    const details = [];
    if (tax.taxable_amount) {
      details.push(`Bemessungsgrundlage ${formatMoney(tax.taxable_amount)}`);
    }
    (tax.exemption?.reasons || []).forEach((reason) => {
      if (present(reason)) details.push(`Begründung: ${reason}`);
    });
    if (tax.exemption?.reason_code) {
      details.push(`Begründungscode: ${codeDisplay(tax.exemption.reason_code)}`);
    }
    const heading = [
      codeDisplay(tax.category, 'Steuer'),
      present(tax.rate_percent) ? `${formatDecimalExact(tax.rate_percent)} %` : null,
      tax.tax_type ? `(${codeDisplay(tax.tax_type)})` : null,
    ].filter(present).join(' · ');
    return `
      <div class="tax-row">
        <div><span>${escapeHtml(heading)}</span>
        ${details.length ? `<small>${escapeHtml(details.join(' · '))}</small>` : ''}</div>
        <strong>${escapeHtml(formatMoney(tax.tax_amount))}</strong>
      </div>`;
  }).join('') : '<p class="empty-state">Keine Steueraufschlüsselung erkannt.</p>';
  const totalRows = [
    ['Umsatzsteuerbetrag (BT-110)', taxTotals.document_currency],
    ['Umsatzsteuerbetrag in Abrechnungswährung (BT-111)', taxTotals.vat_accounting_currency],
  ].filter(([, amount]) => amount);
  if (totalRows.length) {
    html += `${subsectionHeading('Steuersummen')}${totalRows.map(([label, amount]) => `
      <div class="tax-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatMoney(amount))}</strong></div>
    `).join('')}`;
  }
  $('#tax-section').innerHTML = html;
}

function renderTotals(data) {
  const totals = data.totals || {};
  const rows = [
    ['Summe Rechnungspositionen (BT-106)', totals.line_net_total, ''],
    ['Nachlässe', totals.allowance_total, ''],
    ['Zuschläge', totals.charge_total, ''],
    ['Rechnungsbetrag ohne Umsatzsteuer (BT-109)', totals.tax_exclusive_total, ''],
    ['Rechnungsbetrag mit Umsatzsteuer (BT-112)', totals.tax_inclusive_total, 'grand'],
    ['Vorauszahlungen', totals.prepaid_total, ''],
    ['Rundung', totals.rounding, ''],
    ['Ausstehender Betrag (BT-115)', totals.payable, 'payable'],
  ].filter((row) => present(row[1]));
  $('#totals-section').innerHTML = rows.map(([label, value, className]) => `
    <div class="total-row ${className}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatMoney(value))}</strong></div>
  `).join('') || '<p class="empty-state">Keine Summen erkannt.</p>';
}

function renderAdditionalParties(data) {
  const parties = data.parties || {};
  const roles = [
    ['Zahlungsempfänger', parties.payee],
    ['Rechnungsempfänger', parties.invoice_recipient],
    ['Steuervertreter des Verkäufers', parties.seller_tax_representative],
    ['Lieferempfänger', parties.delivery_recipient],
  ].filter(([, party]) => partyHasData(party));
  const section = $('#additional-parties-section');
  section.hidden = roles.length === 0;
  section.innerHTML = roles.map(([label, party]) => `
    <article class="content-card"><h2>${escapeHtml(label)}</h2>${renderParty(party)}</article>
  `).join('');
}

function renderHeaderAdjustments(data) {
  const items = data.allowances_charges || [];
  const card = $('#header-adjustments-card');
  card.hidden = items.length === 0;
  $('#header-adjustments-section').innerHTML = items.map((item, index) => {
    const detail = [
      present(item.percentage) ? `${formatNumber(item.percentage, 4)} %` : null,
      item.base_amount ? `Basis ${formatMoney(item.base_amount)}` : null,
      item.reason_text,
      item.reason_code ? `Code ${codeDisplay(item.reason_code)}` : null,
      item.tax_category ? `Steuer ${codeDisplay(item.tax_category)}` : null,
      present(item.tax_rate_percent) ? `${formatDecimalExact(item.tax_rate_percent)} %` : null,
      present(item.indicator_raw) ? `Indikator ${item.indicator_raw}` : null,
    ].filter(present).join(' · ');
    return `
      <div class="tax-row">
        <div><span>${escapeHtml(allowanceChargeKindLabel(item.kind, index))}</span><small>${escapeHtml(detail)}</small></div>
        <strong>${escapeHtml(formatMoney(item.amount))}</strong>
      </div>`;
  }).join('');
}

function maskCardIdentifier(value) {
  if (!present(value)) return null;
  const visible = String(value).replace(/[•*\s-]/g, '');
  return visible ? `•••• ${visible.slice(-4)}` : '••••';
}

function resolvedRoleLabel(role) {
  return role && role !== 'unknown' ? ROLE_LABELS[role] || null : null;
}

function roleFlow(from, to) {
  const fromLabel = resolvedRoleLabel(from);
  const toLabel = resolvedRoleLabel(to);
  return fromLabel && toLabel ? `${fromLabel} → ${toLabel}` : 'Nicht eindeutig ableitbar';
}

function expectedPaymentFlow(roles) {
  if (roles.expected_payment_direction === 'none') return 'Keine Zahlung erwartet';
  return roleFlow(roles.expected_payer, roles.expected_recipient);
}

function roleSemanticsNote(derivation) {
  const explanation = {
    explicit: 'Aus den strukturierten Rechnungsangaben ermittelt.',
    derived: 'Aus Dokumenttyp, Zahlbetrag und Parteienrollen abgeleitet.',
    ambiguous: 'Wegen widersprüchlicher Angaben nicht eindeutig ableitbar.',
    unknown: 'Mangels hinreichender Angaben nicht eindeutig ableitbar.',
  }[derivation] || 'Mangels hinreichender Angaben nicht eindeutig ableitbar.';
  return `${explanation} Dies ist kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss.`;
}

function paymentSectionHeading(label) {
  return `<h3 class="payment-heading payment-section-heading">${escapeHtml(label)}</h3>`;
}

function paymentItemHeading(label) {
  return `<h4 class="payment-heading payment-item-heading">${escapeHtml(label)}</h4>`;
}

function paymentDetailHeading(label) {
  return `<h5 class="payment-heading payment-detail-heading">${escapeHtml(label)}</h5>`;
}

function paymentDetailBlock(label, rows) {
  return `
    <section class="payment-detail">
      ${paymentDetailHeading(label)}
      ${detailRows(rows)}
    </section>`;
}

function renderPaymentInstruction(instruction, index) {
  const details = [];
  (instruction.credit_transfers || []).forEach((transfer, transferIndex) => {
    details.push(paymentDetailBlock(`Überweisungskonto ${transferIndex + 1}`, [
      ['Konto', identifierDisplay(transfer.account_id, null)],
      ['Kontoinhaber', transfer.account_name],
      ['Zahlungsdienstleister', identifierDisplay(transfer.service_provider_id, null)],
    ]));
  });
  if (instruction.payment_card) {
    details.push(paymentDetailBlock('Zahlungskarte', [
      ['Maskierte Kartenkennung', maskCardIdentifier(instruction.payment_card.masked_account_identifier)],
      ['Karteninhaber', instruction.payment_card.holder_name],
    ]));
  }
  if (instruction.direct_debit) {
    details.push(paymentDetailBlock('Lastschrift', [
      ['Mandatsreferenz', instruction.direct_debit.mandate_reference],
      ['Gläubigerkennung', identifierDisplay(instruction.direct_debit.creditor_id, null)],
      ['Kennung des belasteten Kontos', identifierDisplay(instruction.direct_debit.debited_account_id, null)],
    ]));
  }
  return `
    <article class="payment-item">
      ${paymentItemHeading(`Zahlungsanweisung ${index + 1}`)}
      ${detailRows([
        ['Zahlungsart', codeDisplay(instruction.means, null)],
        ['Hinweis', instruction.instruction_note],
        ['Zahlungs-ID', instruction.payment_id],
      ])}
      ${details.join('')}
    </article>`;
}

function renderPayment(data) {
  const payment = data.payment || {};
  const instructions = payment.instructions || [];
  const terms = payment.terms || [];
  const roles = data.roles || {};
  const referenceRows = present(payment.reference)
    ? detailRows([['Zahlungsreferenz', payment.reference]])
    : '';
  const instructionItems = instructions.length
    ? instructions.map(renderPaymentInstruction).join('')
    : '<p class="empty-state">Keine Zahlungsanweisungen angegeben.</p>';
  const blocks = [`
    <section class="payment-section payment-flow-section">
      ${paymentSectionHeading('Dokument- und Zahlungsfluss')}
      ${detailRows([
        ['Dokumentfluss', roleFlow(roles.issuer, roles.document_recipient)],
        ['Erwarteter Zahlungsfluss', expectedPaymentFlow(roles)],
      ])}
      <p class="muted role-semantics-note">${escapeHtml(roleSemanticsNote(roles.derivation))}</p>
      ${referenceRows}
    </section>
    <section class="payment-section payment-instructions-section">
      ${paymentSectionHeading('Zahlungsanweisungen (BG-16)')}
      <div class="payment-section-content">${instructionItems}</div>
    </section>`];
  terms.forEach((item, index) => {
    blocks.push(`
      <section class="payment-section payment-terms-section">
        ${paymentSectionHeading(`Zahlungsbedingung ${index + 1}`)}
        ${detailRows([
          ['Beschreibung', item.description],
          ['Fälligkeit', formatDate(item.due_date)],
          ['Teilzahlungsbetrag', formatMoney(item.partial_payment)],
        ])}
      </section>`);
  });
  $('#payment-section').innerHTML = blocks.join('');
}

function formatReference(reference) {
  if (!reference) return null;
  return [
    identifierDisplay(reference.id, null),
    reference.issue_date ? `vom ${formatDate(reference.issue_date)}` : null,
    reference.description,
  ].filter(present).join(' · ') || null;
}

function renderReferences(data) {
  const refs = data.references || {};
  const rows = [
    ['Bestellreferenz Käufer', formatReference(refs.buyer_order)],
    ['Bestellreferenz Verkäufer', formatReference(refs.seller_order)],
    ['Vertragsreferenz', formatReference(refs.contract)],
    ['Ausschreibungs-/Losreferenz', formatReference(refs.tender)],
    ['Projektreferenz', formatReference(refs.project)],
    ['Kontierungsreferenz des Käufers', refs.buyer_accounting_reference],
    ['Kennung des fakturierten Objekts', formatReference(refs.invoiced_object)],
    ['Vorgängerrechnungen', (refs.preceding_invoices || []).map(formatReference).filter(present).join(', ')],
    ['Versandavis', formatReference(refs.despatch_advice)],
    ['Wareneingangsavis', formatReference(refs.receiving_advice)],
  ];
  const supportingDocuments = refs.supporting_documents || [];
  let html = detailRows(rows);
  if (supportingDocuments.length) {
    html += subsectionHeading('Belege und Anlagen') + supportingDocuments.map((item) => detailRows([
      ['ID', identifierDisplay(item.id, null)],
      ['Typ', codeDisplay(item.type, null)],
      ['Name', item.name],
      ['Beschreibung', item.description],
      ['Datei', item.attachment_filename],
      ['MIME', item.attachment_mime_type],
      ['Eingebettet', item.embedded ? 'Ja' : 'Nein'],
      ['URI', item.external_uri],
    ])).join('');
  }
  $('#references-section').innerHTML = html;
}

function renderNotes(data) {
  const notes = data.document?.notes || [];
  $('#notes-section').innerHTML = notes.length
    ? notes.map((note) => {
      const subject = note.subject_code ? `<strong>${escapeHtml(codeDisplay(note.subject_code))}</strong><br>` : '';
      return `<p class="note-box">${subject}${escapeHtml(note.text)}</p>`;
    }).join('')
    : '<p class="empty-state">Keine allgemeinen Rechnungshinweise enthalten.</p>';
}

function renderSource(data) {
  const source = data.source || {};
  const container = source.container || {};
  const upload = source.upload;
  const invoiceXml = source.invoice_xml;
  const attachments = source.attachments || [];
  let html = detailRows([
    ['Datei', upload?.filename],
    ['Dateityp', upload?.media_type],
    ['Größe', upload ? formatBytes(upload.size_bytes) : null],
    ['SHA-256 Quelldatei', upload?.sha256],
    ['Container', container.kind],
    ['Seiten', container.page_count],
    ['Gewählte Einbettung', container.selected_attachment],
    ['Anzahl Einbettungen', container.attachment_count],
    ['XML-Datei', invoiceXml?.filename],
    ['XML-Dateityp', invoiceXml?.media_type],
    ['XML-Größe', invoiceXml ? formatBytes(invoiceXml.size_bytes) : null],
    ['SHA-256 XML', invoiceXml?.sha256],
    ['Verarbeitung', present(data.runtime?.duration_ms) ? `${formatNumber(data.runtime.duration_ms, 2)} ms` : null],
    ['Erstellt', data.runtime?.generated_at],
    ['Anwendungsversion', data.runtime?.application_version],
  ]);
  if (attachments.length) {
    html += subsectionHeading('Eingebettete Dateien') + attachments.map((item) => detailRows([
      ['Name', item.name],
      ['Größe', formatBytes(item.size_bytes)],
      ['XML', item.is_xml ? 'Ja' : 'Nein'],
      ['Ausgewählt', item.selected ? 'Ja' : 'Nein'],
      ['SHA-256', item.sha256],
    ])).join('');
  }
  $('#source-section').innerHTML = html;
}

function findingIcon(severity) {
  return severity === 'error' ? '×' : severity === 'warning' ? '!' : 'i';
}

const OCCURRENCE_SCOPE_LABELS = {
  document: 'Dokument',
  profile: 'Profil',
  party: 'Partei',
  period: 'Zeitraum',
  reference: 'Referenz',
  line: 'Rechnungsposition',
  'allowance-charge': 'Nachlass/Zuschlag',
  tax: 'Steuer',
  total: 'Summe',
  payment: 'Zahlung',
  source: 'Quelle',
  technical: 'Technik',
  runtime: 'Verarbeitung',
};

function semanticReferenceDisplay(reference) {
  if (!reference || !present(reference.id)) return null;
  return present(reference.label)
    ? `${reference.label} (${reference.id})`
    : `Semantische Referenz ${reference.id}`;
}

function evidenceDisplay(evidence) {
  if (!evidence || !present(evidence.value)) return null;
  return `${evidence.value}${present(evidence.unit) ? ` ${evidence.unit}` : ''}`;
}

function renderFinding(item) {
  const rule = item.rule || {};
  const occurrence = item.occurrence;
  const xmlLocation = item.xml_location;
  const semanticReferences = (item.semantic_references || [])
    .map(semanticReferenceDisplay)
    .filter(present);
  const occurrenceDescription = occurrence
    ? [
      OCCURRENCE_SCOPE_LABELS[occurrence.scope] || occurrence.scope,
      present(occurrence.index) ? `Nr. ${Number(occurrence.index) + 1}` : null,
      occurrence.identifier,
    ].filter(present).join(' · ')
    : null;
  const xmlDescription = xmlLocation
    ? [
      present(xmlLocation.path) ? `XML-Pfad: ${xmlLocation.path}` : null,
      xmlLocation.line ? `Zeile ${xmlLocation.line}` : null,
      xmlLocation.column ? `Spalte ${xmlLocation.column}` : null,
    ].filter(present).join(' · ')
    : null;
  const meta = [
    semanticReferences.length ? `Semantik: ${semanticReferences.join(', ')}` : null,
    occurrenceDescription ? `Vorkommen: ${occurrenceDescription}` : null,
    occurrence?.json_pointer ? `JSON-Pointer: ${occurrence.json_pointer}` : null,
    xmlDescription,
    evidenceDisplay(item.actual) ? `Ist: ${evidenceDisplay(item.actual)}` : null,
    evidenceDisplay(item.expected) ? `Erwartet: ${evidenceDisplay(item.expected)}` : null,
    rule.source ? `Quelle: ${rule.source}` : null,
    rule.reference ? `Regelreferenz: ${rule.reference}` : null,
    rule.profile ? `Profil: ${rule.profile}` : null,
    rule.version ? `Version: ${rule.version}` : null,
  ].filter(present);
  return `
    <article class="finding ${escapeHtml(item.severity || 'info')}">
      <span class="finding-icon" aria-hidden="true">${findingIcon(item.severity)}</span>
      <div>
        <h3>${escapeHtml(text(rule.title, 'Prüfmeldung'))}</h3>
        <p>${escapeHtml(text(rule.message, ''))}</p>
        ${meta.length ? `<div class="finding-meta">${meta.map((entry) => `<span>${escapeHtml(entry)}</span>`).join('')}</div>` : ''}
      </div>
      <span class="finding-code">${escapeHtml(text(rule.id, '–'))}</span>
    </article>`;
}

function renderProcessingLimitation(item) {
  const meta = item.affected_json_pointer
    ? `<div class="finding-meta"><span>${escapeHtml(`JSON-Pointer: ${item.affected_json_pointer}`)}</span></div>`
    : '';
  return `
    <article class="finding info">
      <span class="finding-icon" aria-hidden="true">i</span>
      <div><h3>Verarbeitungsbegrenzung</h3><p>${escapeHtml(item.message)}</p>${meta}</div>
      <span class="finding-code">${escapeHtml(item.code)}</span>
    </article>`;
}

function renderAxisFindings(label, axis, limitations = []) {
  const findings = axis.findings || [];
  const content = [
    ...findings.map(renderFinding),
    ...limitations.map(renderProcessingLimitation),
  ].join('');
  return `
    <section class="content-card wide-card">
      <div class="section-heading">
        <div><p class="eyebrow">${escapeHtml(label)}</p><h2>${escapeHtml(text(axis.summary, 'Keine Zusammenfassung vorhanden.'))}</h2></div>
      </div>
      ${content ? `<div class="findings-list">${content}</div>` : '<p class="empty-state">Keine Befunde auf dieser Achse.</p>'}
    </section>`;
}

function renderValidation(data) {
  const assessment = data.assessment || {};
  const official = assessment.official || {};
  const internal = assessment.internal || {};
  const processing = assessment.processing || {};
  $('#validation-assessment').textContent = 'Prüfergebnis nach drei getrennten Achsen';
  $('#builtin-scope').textContent = internal.scope
    || 'Offizielle Konformität, interne Prüfungen und technische Verarbeitung werden unabhängig voneinander ausgewiesen.';
  $('#official-state').innerHTML = [
    ['Offizielle Konformitätsprüfung', OFFICIAL_STATUS_LABELS[official.status], official.summary],
    ['Interne Prüfung', INTERNAL_STATUS_LABELS[internal.status], internal.summary],
    ['Verarbeitung', PROCESSING_STATUS_LABELS[processing.status], processing.summary],
  ].map(([label, status, summary]) => `
    <div><strong>${escapeHtml(label)}: ${escapeHtml(text(status, 'Unbekannt'))}</strong><span>${escapeHtml(text(summary, 'Keine Zusammenfassung.'))}</span></div>
  `).join('');

  $('#findings-list').innerHTML = [
    renderAxisFindings('Offizielle Konformitätsprüfung', official),
    renderAxisFindings('Interne Prüfung', internal),
    renderAxisFindings('Verarbeitung', processing, processing.limitations || []),
  ].join('');

  const details = $('#official-report-details');
  const technicalReport = [
    official.raw_report,
    official.technical_output,
  ].filter(present).join('\n\n');
  details.hidden = !technicalReport;
  $('#official-report-raw').textContent = technicalReport;
}

function filteredTechnicalRows() {
  const query = ($('#technical-search').value || '').trim().toLowerCase();
  const rows = state.analysis?.technical?.fields || [];
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
  $('#raw-xml').textContent = technical.source_xml || '';
}

function renderAll(data) {
  state.analysis = data;
  renderSummary(data);
  renderFacts(data);
  $('#seller-card').innerHTML = renderParty(data.parties?.seller);
  $('#buyer-card').innerHTML = renderParty(data.parties?.buyer);
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
  $('.upload-card').classList.toggle('is-loading', loading);
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
  try {
    const form = new FormData();
    form.append('file', file, file.name);
    form.append('official', officialValidationRequested() ? 'true' : 'false');
    const response = await uiFetch('/api/analyze', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await parseError(response));
    const data = await response.json();
    if (data.schema_version !== 2) {
      throw new Error('Die Serverantwort entspricht nicht dem erwarteten Analyse-Schema 2.');
    }
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
    const response = await uiFetch('/api/xml', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await parseError(response));
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || state.analysis.source?.invoice_xml?.filename || 'rechnung.xml';
    downloadBlob(blob, safeFilename(filename, 'rechnung.xml'));
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function fetchHtmlReport(scope = 'readable') {
  if (!state.file) throw new Error('Keine Rechnung geladen.');
  const form = new FormData();
  form.append('file', state.file, state.file.name);
  form.append('official', officialValidationRequested() ? 'true' : 'false');
  form.append('scope', scope);
  const response = await uiFetch('/api/report', { method: 'POST', body: form });
  if (!response.ok) throw new Error(await parseError(response));
  return response.blob();
}

async function downloadHtml() {
  try {
    const blob = await fetchHtmlReport('readable');
    const id = state.analysis?.document?.id || 'bericht';
    downloadBlob(blob, `${safeFilename(id)}-lesbare-e-rechnung.html`);
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function downloadCompleteHtml() {
  try {
    const blob = await fetchHtmlReport('complete');
    const id = state.analysis?.document?.id || 'bericht';
    downloadBlob(blob, `${safeFilename(id)}-vollstaendiger-e-rechnungsbericht.html`);
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
    const blob = await fetchHtmlReport('readable');
    const url = URL.createObjectURL(blob);
    let cleanedUp = false;
    const cleanup = () => {
      if (cleanedUp) return;
      cleanedUp = true;
      URL.revokeObjectURL(url);
    };
    const cleanupFallback = setTimeout(cleanup, 60_000);
    printWindow.addEventListener('afterprint', () => {
      clearTimeout(cleanupFallback);
      cleanup();
    }, { once: true });
    printWindow.addEventListener('load', () => {
      try { printWindow.focus(); printWindow.print(); } catch (_error) { /* The report remains open for manual printing. */ }
    }, { once: true });
    printWindow.location.href = url;
  } catch (error) {
    printWindow.close();
    window.alert(error instanceof Error ? error.message : String(error));
  }
}

async function copyXml() {
  const xml = state.analysis?.technical?.source_xml || '';
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
    const response = await uiFetch(`/api/examples/${encodeURIComponent(name)}`);
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
  if (!uiContractIsUsable()) return;

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
  $('#download-complete-html-button').addEventListener('click', downloadCompleteHtml);
  $('#print-button').addEventListener('click', printReport);
  $('#copy-xml-button').addEventListener('click', copyXml);

  $('#technical-search').addEventListener('input', () => renderTechnicalPage(true));
  $('#technical-prev').addEventListener('click', () => { state.technicalPage -= 1; renderTechnicalPage(); });
  $('#technical-next').addEventListener('click', () => { state.technicalPage += 1; renderTechnicalPage(); });
}

document.addEventListener('DOMContentLoaded', initialise);
