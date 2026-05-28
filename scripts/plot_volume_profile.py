import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter

SESSION_ID = "2026-04-28"
OUTPUT_PATH = "volume_profile_2026-04-28.jpg"

VOLUME_PROFILE = [
    {"bin_index": 0, "bin_low": 75635.6, "bin_high": 75680.35, "buy_volume": 542.478, "sell_volume": 678.267, "total_volume": 1220.745, "delta": -135.789},
    {"bin_index": 1, "bin_low": 75680.35, "bin_high": 75725.11, "buy_volume": 593.728, "sell_volume": 602.055, "total_volume": 1195.783, "delta": -8.327},
    {"bin_index": 2, "bin_low": 75725.11, "bin_high": 75769.86, "buy_volume": 872.163, "sell_volume": 720.945, "total_volume": 1593.108, "delta": 151.218},
    {"bin_index": 3, "bin_low": 75769.86, "bin_high": 75814.61, "buy_volume": 969.246, "sell_volume": 926.94, "total_volume": 1896.186, "delta": 42.306},
    {"bin_index": 4, "bin_low": 75814.61, "bin_high": 75859.36, "buy_volume": 929.465, "sell_volume": 1023.643, "total_volume": 1953.108, "delta": -94.178},
    {"bin_index": 5, "bin_low": 75859.36, "bin_high": 75904.11, "buy_volume": 1355.198, "sell_volume": 1199.774, "total_volume": 2554.972, "delta": 155.424},
    {"bin_index": 6, "bin_low": 75904.11, "bin_high": 75948.87, "buy_volume": 1360.819, "sell_volume": 1140.576, "total_volume": 2501.395, "delta": 220.243},
    {"bin_index": 7, "bin_low": 75948.87, "bin_high": 75993.62, "buy_volume": 1795.774, "sell_volume": 1732.95, "total_volume": 3528.724, "delta": 62.824},
    {"bin_index": 8, "bin_low": 75993.62, "bin_high": 76038.37, "buy_volume": 1726.804, "sell_volume": 1507.201, "total_volume": 3234.005, "delta": 219.603},
    {"bin_index": 9, "bin_low": 76038.37, "bin_high": 76083.12, "buy_volume": 1111.544, "sell_volume": 909.041, "total_volume": 2020.585, "delta": 202.503},
    {"bin_index": 10, "bin_low": 76083.12, "bin_high": 76127.87, "buy_volume": 1375.645, "sell_volume": 1085.623, "total_volume": 2461.268, "delta": 290.022},
    {"bin_index": 11, "bin_low": 76127.87, "bin_high": 76172.63, "buy_volume": 1973.196, "sell_volume": 1540.431, "total_volume": 3513.627, "delta": 432.765},
    {"bin_index": 12, "bin_low": 76172.63, "bin_high": 76217.38, "buy_volume": 2181.882, "sell_volume": 1760.067, "total_volume": 3941.949, "delta": 421.815},
    {"bin_index": 13, "bin_low": 76217.38, "bin_high": 76262.13, "buy_volume": 1468.241, "sell_volume": 1553.462, "total_volume": 3021.703, "delta": -85.221},
    {"bin_index": 14, "bin_low": 76262.13, "bin_high": 76306.88, "buy_volume": 2446.83, "sell_volume": 2239.506, "total_volume": 4686.336, "delta": 207.324},
    {"bin_index": 15, "bin_low": 76306.88, "bin_high": 76351.63, "buy_volume": 1964.486, "sell_volume": 2139.113, "total_volume": 4103.599, "delta": -174.627},
    {"bin_index": 16, "bin_low": 76351.63, "bin_high": 76396.39, "buy_volume": 1357.841, "sell_volume": 1239.489, "total_volume": 2597.33, "delta": 118.352},
    {"bin_index": 17, "bin_low": 76396.39, "bin_high": 76441.14, "buy_volume": 970.966, "sell_volume": 899.664, "total_volume": 1870.63, "delta": 71.302},
    {"bin_index": 18, "bin_low": 76441.14, "bin_high": 76485.89, "buy_volume": 670.193, "sell_volume": 592.507, "total_volume": 1262.7, "delta": 77.686},
    {"bin_index": 19, "bin_low": 76485.89, "bin_high": 76530.64, "buy_volume": 831.502, "sell_volume": 835.045, "total_volume": 1666.547, "delta": -3.543},
    {"bin_index": 20, "bin_low": 76530.64, "bin_high": 76575.39, "buy_volume": 714.872, "sell_volume": 514.091, "total_volume": 1228.963, "delta": 200.781},
    {"bin_index": 21, "bin_low": 76575.39, "bin_high": 76620.15, "buy_volume": 512.714, "sell_volume": 197.444, "total_volume": 710.158, "delta": 315.27},
    {"bin_index": 22, "bin_low": 76620.15, "bin_high": 76664.9, "buy_volume": 290.991, "sell_volume": 46.243, "total_volume": 337.234, "delta": 244.748},
    {"bin_index": 23, "bin_low": 76664.9, "bin_high": 76709.65, "buy_volume": 190.433, "sell_volume": 141.742, "total_volume": 332.175, "delta": 48.691},
    {"bin_index": 24, "bin_low": 76709.65, "bin_high": 76754.4, "buy_volume": 127.177, "sell_volume": 84.986, "total_volume": 212.163, "delta": 42.191},
    {"bin_index": 25, "bin_low": 76754.4, "bin_high": 76799.15, "buy_volume": 452.97, "sell_volume": 441.809, "total_volume": 894.779, "delta": 11.161},
    {"bin_index": 26, "bin_low": 76799.15, "bin_high": 76843.91, "buy_volume": 850.548, "sell_volume": 735.593, "total_volume": 1586.141, "delta": 114.955},
    {"bin_index": 27, "bin_low": 76843.91, "bin_high": 76888.66, "buy_volume": 733.908, "sell_volume": 665.68, "total_volume": 1399.588, "delta": 68.228},
    {"bin_index": 28, "bin_low": 76888.66, "bin_high": 76933.41, "buy_volume": 1644.526, "sell_volume": 1899.381, "total_volume": 3543.907, "delta": -254.855},
    {"bin_index": 29, "bin_low": 76933.41, "bin_high": 76978.16, "buy_volume": 1880.496, "sell_volume": 2009.158, "total_volume": 3889.654, "delta": -128.662},
    {"bin_index": 30, "bin_low": 76978.16, "bin_high": 77022.91, "buy_volume": 3935.163, "sell_volume": 3826.936, "total_volume": 7762.099, "delta": 108.227},
    {"bin_index": 31, "bin_low": 77022.91, "bin_high": 77067.67, "buy_volume": 2245.236, "sell_volume": 2164.29, "total_volume": 4409.526, "delta": 80.946},
    {"bin_index": 32, "bin_low": 77067.67, "bin_high": 77112.42, "buy_volume": 1974.265, "sell_volume": 1724.263, "total_volume": 3698.528, "delta": 250.002},
    {"bin_index": 33, "bin_low": 77112.42, "bin_high": 77157.17, "buy_volume": 1895.829, "sell_volume": 1843.51, "total_volume": 3739.339, "delta": 52.319},
    {"bin_index": 34, "bin_low": 77157.17, "bin_high": 77201.92, "buy_volume": 2069.809, "sell_volume": 1761.182, "total_volume": 3830.991, "delta": 308.627},
    {"bin_index": 35, "bin_low": 77201.92, "bin_high": 77246.67, "buy_volume": 1927.516, "sell_volume": 2344.9, "total_volume": 4272.416, "delta": -417.384},
    {"bin_index": 36, "bin_low": 77246.67, "bin_high": 77291.43, "buy_volume": 1480.174, "sell_volume": 1490.576, "total_volume": 2970.75, "delta": -10.402},
    {"bin_index": 37, "bin_low": 77291.43, "bin_high": 77336.18, "buy_volume": 1390.811, "sell_volume": 1099.778, "total_volume": 2490.589, "delta": 291.033},
    {"bin_index": 38, "bin_low": 77336.18, "bin_high": 77380.93, "buy_volume": 1018.12, "sell_volume": 787.48, "total_volume": 1805.6, "delta": 230.64},
    {"bin_index": 39, "bin_low": 77380.93, "bin_high": 77425.68, "buy_volume": 820.494, "sell_volume": 995.855, "total_volume": 1816.349, "delta": -175.361},
    {"bin_index": 40, "bin_low": 77425.68, "bin_high": 77470.43, "buy_volume": 798.609, "sell_volume": 782.634, "total_volume": 1581.243, "delta": 15.975},
    {"bin_index": 41, "bin_low": 77470.43, "bin_high": 77515.19, "buy_volume": 773.596, "sell_volume": 714.951, "total_volume": 1488.547, "delta": 58.645},
    {"bin_index": 42, "bin_low": 77515.19, "bin_high": 77559.94, "buy_volume": 1189.782, "sell_volume": 1157.377, "total_volume": 2347.159, "delta": 32.405},
    {"bin_index": 43, "bin_low": 77559.94, "bin_high": 77604.69, "buy_volume": 1546.006, "sell_volume": 1598.121, "total_volume": 3144.127, "delta": -52.115},
    {"bin_index": 44, "bin_low": 77604.69, "bin_high": 77649.44, "buy_volume": 1509.223, "sell_volume": 1552.443, "total_volume": 3061.666, "delta": -43.22},
    {"bin_index": 45, "bin_low": 77649.44, "bin_high": 77694.2, "buy_volume": 1981.397, "sell_volume": 2137.132, "total_volume": 4118.529, "delta": -155.735},
    {"bin_index": 46, "bin_low": 77694.2, "bin_high": 77738.95, "buy_volume": 1383.011, "sell_volume": 1452.172, "total_volume": 2835.183, "delta": -69.161},
    {"bin_index": 47, "bin_low": 77738.95, "bin_high": 77783.7, "buy_volume": 771.662, "sell_volume": 724.376, "total_volume": 1496.038, "delta": 47.286},
    {"bin_index": 48, "bin_low": 77783.7, "bin_high": 77828.45, "buy_volume": 556.387, "sell_volume": 548.387, "total_volume": 1104.774, "delta": 8.0},
    {"bin_index": 49, "bin_low": 77828.45, "bin_high": 77873.2, "buy_volume": 269.012, "sell_volume": 105.635, "total_volume": 374.647, "delta": 163.377},
]


def plot_two_sided_volume_profile(output_path: str = OUTPUT_PATH) -> None:
    bins = [row["bin_index"] for row in VOLUME_PROFILE]
    total_volumes = [row["total_volume"] for row in VOLUME_PROFILE]
    # Force delta to left side (negative x direction)
    delta_left = [-abs(row["delta"]) for row in VOLUME_PROFILE]
    delta_colors = ["#2ca02c" if row["delta"] >= 0 else "#d62728" for row in VOLUME_PROFILE]

    plt.figure(figsize=(14, 10))
    plt.barh(bins, total_volumes, color="#1f77b4", alpha=0.75, label="Total Volume (Right)")
    plt.barh(bins, delta_left, color=delta_colors, alpha=0.75, label="Delta Magnitude (Left)")

    plt.axvline(0, color="black", linewidth=1)
    plt.title(f"Two-Sided Volume Profile ({SESSION_ID}) - 50 Bins")
    plt.xlabel("Left: Delta | Right: Total Volume")
    plt.ylabel("Bin Index")
    plt.grid(axis="x", alpha=0.25)
    plt.legend(loc="lower right")
    plt.gca().xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, format="jpg")
    plt.close()


def main() -> None:
    plot_two_sided_volume_profile(OUTPUT_PATH)
    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
