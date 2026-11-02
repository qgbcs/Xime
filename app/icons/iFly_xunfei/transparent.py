import os
import sys
import requests
from io import BytesIO

# ---------- 配置区 ----------
IMAGE_URL = "https://android-artworks.25pp.com/fs08/2026/06/25/11/110_dd3174f3527cd4744059dafaca52babc_con.png"
OUTPUT_PATH = "512.png"
# 回退模式的白色阈值：RGB 均大于此值被视为背景
WHITE_THRESHOLD = 240
# ---------------------------

def download_image(url: str) -> bytes:
    """下载图片并返回二进制内容"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def remove_bg_with_rembg(image_bytes: bytes) -> bytes:
    """ pip install "rembg[cpu]"
 初次运行下载1.02GB大模型   使用 rembg AI 模型去除背景，返回透明 PNG 字节"""
    from rembg import remove
    return remove(image_bytes)

def remove_bg_with_pillow(image_bytes: bytes, threshold: int) -> bytes:
    """回退方案：将接近白色的像素设为透明，并平滑边缘"""
    from PIL import Image, ImageFilter

    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    datas = img.getdata()
    new_data = []

    for r, g, b, a in datas:
        # 计算与纯白的欧几里得距离
        dist = ((255 - r) ** 2 + (255 - g) ** 2 + (255 - b) ** 2) ** 0.5
        if dist < 20:
            # 非常接近白色 → 完全透明
            new_data.append((255, 255, 255, 0))
        elif dist < 55:
            # 过渡区 → 按距离线性分配 alpha，避免锯齿
            alpha = int(((dist - 20) / (55 - 20)) * 255)
            new_data.append((r, g, b, alpha))
        else:
            new_data.append((r, g, b, a))

    img.putdata(new_data)

    # 对 alpha 通道做轻微模糊，让边缘更平滑
    alpha = img.getchannel("A")
    alpha = alpha.filter(ImageFilter.BoxBlur(1))
    img.putalpha(alpha)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def main():
    print(f"正在下载: {IMAGE_URL}")
    try:
        img_bytes = download_image(IMAGE_URL)
    except Exception as e:
        print(f"下载失败: {e}")
        sys.exit(1)

    print("下载完成，开始去除背景...")

    # 优先尝试 rembg
    try:
        result = remove_bg_with_rembg(img_bytes)
        print("✅ 使用 rembg (AI) 成功去除背景")
    except ImportError:
        print("⚠️ 未安装 rembg，回退到 Pillow 阈值法")
        result = remove_bg_with_pillow(img_bytes, WHITE_THRESHOLD)
        print("✅ 使用 Pillow 阈值法成功去除背景")
    except Exception as e:
        print(f"rembg 处理失败: {e}，回退到 Pillow 阈值法")
        result = remove_bg_with_pillow(img_bytes, WHITE_THRESHOLD)

    with open(OUTPUT_PATH, "wb") as f:
        f.write(result)
    print(f"透明图标已保存: {OUTPUT_PATH}")

if __name__ == "__main__":
    main()