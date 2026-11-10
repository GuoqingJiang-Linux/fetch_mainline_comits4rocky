#!/bin/bash
# 使用 diff -qr 找出所有内容不同的文件，并循环处理
CURR_PATH=/home/gjiang/sources/get-mainline-commits-from-rocky
# 不参与比较的目录/文件：
#   不含 / 的按 basename 匹配（diff -x 规则）
#   含 / 的按相对路径前缀匹配
EXCLUDE_DIRS=(
    .clang-format
    .git
    .gitignore
    .gitlab-ci.yml
    CREDITS
    Documentation
    Makefile
    Makefile.rhelver
    MAINTAINERS
    arch/arm64/boot/dts
    arch/arm64/configs/defconfig
    arch/loongarch
    arch/riscv
    arch/s390
    drivers/base/regmap
    drivers/bluetooth
    drivers/clk
    drivers/cxl
    drivers/gpio
    drivers/gpu
    drivers/hid
    drivers/hv
    drivers/i2c
    drivers/input
    #是否使用RDMA?
    drivers/infiniband
    drivers/leds
    drivers/misc
    drivers/media
    drivers/memstick
    drivers/mfd
    drivers/mmc
    drivers/net/can
    drivers/net/ethernet/amazon
    drivers/net/ethernet/aquantia
    drivers/net/ethernet/atheros
    drivers/net/ethernet/cavium
    drivers/net/ethernet/chelsio
    drivers/net/ethernet/freescale

    # use out of tree module
    drivers/net/ethernet/intel/i40e
    drivers/net/ethernet/intel/ice

    drivers/net/ethernet/marvell
    drivers/net/ethernet/micrel
    drivers/net/ethernet/microchip
    drivers/net/ethernet/microsoft
    drivers/net/ethernet/netronome
    drivers/net/ethernet/pensando
    drivers/net/ethernet/realtek
    drivers/net/ethernet/renesas
    drivers/net/ethernet/stmicro
    drivers/net/ethernet/ti
    drivers/net/phy
    drivers/net/usb
    drivers/net/wan
    drivers/net/wireguard
    drivers/net/wireless
    drivers/net/wwan
    drivers/nvdimm
    drivers/of
    drivers/phy
    drivers/pinctrl
    drivers/platform/chrome
    drivers/s390
    # we use out of tree scsi drivers
    drivers/scsi
    drivers/soc
    drivers/spi
    drivers/staging
    drivers/soundwire
    drivers/thermal/qcom
    drivers/thunderbolt
    drivers/tty
    drivers/ufs
    drivers/usb
    drivers/video
    drivers/w1
    drivers/watchdog
    drivers/xen
    fs/adfs
    fs/afs
    fs/affs
    fs/autofs
    fs/bcachefs
    fs/bfs
    fs/btrfs
    fs/cifs
    fs/cramfs
    fs/ceph
    fs/exfat
    fs/f2fs
    fs/fat
    fs/jfs
    fs/gfs2
    fs/nilfs2
    fs/ntfs
    fs/ocfs2
    fs/smb
    include/drm
    include/dt-bindings
    include/unit
    include/linux/ieee80211.h
    include/net/bluetooth
    include/net/cfg80211.h
    include/net/mac80211.h
    include/uapi/linux/nl80211.h
    include/sound
    include/uapi
    include/xen
    kernel/printk
    lib/kunit
    mm/damon
    mm/kasan
    net/8021q
    net/bluetooth
    net/mac80211
    net/tipc
    net/wireless
    redhat
    samples
    scripts
    sound
    tools
    usr
)
rm -rf $CURR_PATH/kernel_diffs
mkdir $CURR_PATH/kernel_diffs
cd /home/gjiang/sources

diff_excludes=()
path_excludes=()
for name in "${EXCLUDE_DIRS[@]}"; do
    case "$name" in
        */*) path_excludes+=("$name") ;;
        *)   diff_excludes+=(-x "$name") ;;
    esac
done

diff -qr "${diff_excludes[@]}" centos-stream-9/ linux-5.14.0-427.28.1.el9_4/ | while read -r line; do
    if echo "$line" | grep -q "Files .* and .* differ"; then
        # 提取相对路径
        rel_path=$(echo "$line" | awk '{print $2}' | sed 's|^centos-stream-9/||')

        # 跳过按相对路径排除的目录
        skip=0
        for p in "${path_excludes[@]}"; do
            case "$rel_path" in
                "$p"/*) skip=1; break ;;
            esac
        done
        [ "$skip" -eq 1 ] && continue

        # arch 目录下只比较 x86 和 arm64，跳过其他架构子目录
        case "$rel_path" in
            arch/*/*)
                case "$rel_path" in
                    arch/x86/*|arch/arm64/*) ;;
                    *) continue ;;
                esac
                ;;
        esac

	echo $rel_path
        clean_name=$(echo "$rel_path" | tr '/' '_')

        # 生成单文件 diff
        diff -uN centos-stream-9/"$rel_path" linux-5.14.0-427.28.1.el9_4/"$rel_path" > $CURR_PATH/kernel_diffs/"$clean_name".patch
    fi
done
cd $CURR_PATH
