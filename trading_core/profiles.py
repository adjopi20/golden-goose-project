"""Existing equal-width contiguous value-area calculation, independent of session."""


def value_area(prices, profile_bins=50, value_fraction=.70):
    if not prices:
        raise ValueError("No profile prices")
    if type(profile_bins) is not int or profile_bins < 1 or not 0 < value_fraction <= 1:
        raise ValueError("Invalid profile configuration")
    low, high = min(prices), max(prices)
    weights = [0.] * profile_bins
    width = (high-low) / profile_bins
    # Preserve the caller's accumulation order and existing left-first tie rules.
    for price, qty in prices.items():
        index = min(profile_bins - 1, int((price-low)/width)) if width else 0
        weights[index] += qty
    poc_bin = max(range(profile_bins), key=lambda i: weights[i])
    left = right = poc_bin
    held = weights[poc_bin]
    target = sum(weights) * value_fraction
    while held < target and (left > 0 or right < profile_bins-1):
        if left == 0 or (right < profile_bins-1 and weights[right+1] > weights[left-1]):
            right += 1
            held += weights[right]
        else:
            left -= 1
            held += weights[left]
    return dict(val=low + left*width, poc=low + (poc_bin+.5)*width if width else low,
                vah=low + (right+1)*width if width else high,
                profile_trade_volume=sum(weights), profile_bins=profile_bins,
                value_fraction=value_fraction)
