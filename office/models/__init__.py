"""Models for the VBWD Office bundle.

S147-1 (VBWD Space) ships four: ``OfficeNode`` (the tree — folder or
document), ``OfficeDocument`` (document-only facts + the sealed envelope
data key), ``OfficeVersion`` (append-only content history), and
``OfficeUsage`` (materialised per-user storage total). S147-2 (sharing) adds
``OfficeShare`` (the capability grant) and ``OfficeShareAccess`` (its
append-only audit trail). S147-3 (Docs editor + AI helper) adds
``OfficeEditLease`` (the single-writer soft lock) and ``OfficeAiCall`` (the
AI-helper call audit trail / budget read side).
"""
