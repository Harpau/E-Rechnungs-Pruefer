from __future__ import annotations

CII_ROOT_NAMESPACE = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
CII_NAMESPACES = {
    "rsm": CII_ROOT_NAMESPACE,
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
}

UBL_ROOT_NAMESPACES = {
    "Invoice": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "CreditNote": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
}
UBL_NAMESPACES = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


__all__ = [
    "CII_NAMESPACES",
    "CII_ROOT_NAMESPACE",
    "UBL_NAMESPACES",
    "UBL_ROOT_NAMESPACES",
]
