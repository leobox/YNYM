def get_bias_warnings(trade_count=0, repeated_samples=False):
    """
    Auto-attaches warnings to every report:
    - Selection/survivorship bias (fixed current universe)
    - 15:00-15:30 gap impact
    - Delisted stocks not included
    - Trade count < threshold
    - Historical universe not restored
    - Repeated sample periods
    """
    warnings = [
        "WARNING: Selection/survivorship bias (fixed current universe) is present.",
        "WARNING: 15:00-15:30 gap impact is not fully accounted for.",
        "WARNING: Delisted stocks are not included in the dataset.",
        "WARNING: Historical universe has not been restored."
    ]
    
    if trade_count < 8:
        warnings.append(f"WARNING: Trade count ({trade_count}) < threshold (8). Insufficient sample size.")
        
    if repeated_samples:
        warnings.append("WARNING: Repeated sample periods detected.")
        
    return "\n".join(warnings)
