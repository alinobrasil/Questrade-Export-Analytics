"""Asset type of each holding. Questrade doesn't provide this, so common funds are listed here
and anything else can be set in config.json "asset_classes" (a type name, or a split)."""

EQUITIES, BONDS, CASH, OTHER = "Equities", "Bonds", "Cash", "Other"
ORDER = [EQUITIES, BONDS, OTHER, CASH]

_EQ = {EQUITIES: 1.0}
_BD = {BONDS: 1.0}
_OT = {OTHER: 1.0}

BUILTIN = {
    # US-listed equity ETFs
    **{s: _EQ for s in ["VOO", "VTI", "VXUS", "VEA", "VWO", "SPY", "IVV", "QQQ", "SCHB", "SCHD", "FNDB",
                        "ITOT", "IXUS", "IEFA", "IEMG", "VT", "VUG", "VTV", "VIG", "VYM", "DIA", "IWM"]},
    # Canadian-listed equity ETFs
    **{s + ".TO": _EQ for s in ["VFV", "VUN", "VCN", "VGG", "VIU", "VEE", "VEQT", "XEQT", "ZEQT", "XIC",
                                "XIU", "XUS", "XUU", "XEF", "XEC", "XAW", "ZSP", "ZCN", "ZEA", "HXT",
                                "HXS", "TEC", "VRE", "XRE", "ZRE", "XEI", "ZDV", "VDY"]},
    # Bond ETFs
    **{s: _BD for s in ["BSV", "BND", "BNDX", "AGG", "VGSH", "VGIT", "SHY", "IEF", "TLT", "SGOV", "BIL"]},
    **{s + ".TO": _BD for s in ["VAB", "VSB", "VSC", "VBU", "VBG", "XBB", "XSB", "ZAG", "ZFS", "XSH",
                                "ZST", "HBB", "CASH", "PSA", "HSAV"]},
    # Asset-allocation ETFs: split by their target mix
    **{s + ".TO": {EQUITIES: 0.8, BONDS: 0.2} for s in ["VGRO", "XGRO", "ZGRO"]},
    **{s + ".TO": {EQUITIES: 0.6, BONDS: 0.4} for s in ["VBAL", "XBAL", "ZBAL"]},
    **{s + ".TO": {EQUITIES: 0.4, BONDS: 0.6} for s in ["VCNS", "XCNS", "ZCON"]},
    **{s + ".TO": {EQUITIES: 0.2, BONDS: 0.8} for s in ["VCIP", "XINC"]},
    # Crypto, commodities, currency
    **{s: _OT for s in ["IBIT", "FBTC", "GBTC", "ETHA", "GLD", "IAU", "USO", "SLV"]},
    **{s + ".TO": _OT for s in ["BTCX.B", "BTCC.B", "FBTC", "ETHX.B", "CGL.C", "DLR", "DLR.U"]},
}


def split(symbol, overrides):
    """{asset type: fraction} for a symbol, or None if unknown."""
    o = overrides.get(symbol)
    if isinstance(o, str):
        return {o: 1.0}
    if isinstance(o, dict):
        return o
    return BUILTIN.get(symbol)
