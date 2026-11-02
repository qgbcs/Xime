import unittest

from extract_apk_icon import (
    FdroidSearchParser,
    IconVariant,
    image_dimensions,
    parse_aapt_icons,
    variant_sort_key,
)


class ApkIconTests(unittest.TestCase):
    def test_aapt_density_variants_are_grouped_by_resource_path(self):
        output = "\n".join(
            [
                "application-icon-160:'res/drawable-mdpi/icon.png'",
                "application-icon-320:'res/drawable-xhdpi/icon.png'",
                "application-icon-480:'res/drawable-xxhdpi/icon-hd.png'",
                "application: label='Example' icon='res/drawable-mdpi/icon.png'",
            ]
        )

        self.assertEqual(
            parse_aapt_icons(output),
            {
                "res/drawable-mdpi/icon.png": {160},
                "res/drawable-xhdpi/icon.png": {320},
                "res/drawable-xxhdpi/icon-hd.png": {480},
            },
        )

    def test_png_dimensions(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (192).to_bytes(4, "big") + (128).to_bytes(4, "big")

        self.assertEqual(image_dimensions(data, ".png"), (192, 128))

    def test_lossless_webp_dimensions(self):
        # VP8L packs width-1 and height-1 into three little-endian dimension bytes.
        data = b"RIFF" + b"\0" * 4 + b"WEBPVP8L" + b"\0" * 4 + b"\x2f\xff\xff\x4a\x00"

        self.assertEqual(image_dimensions(data, ".webp"), (16384, 300))

    def test_variant_prefers_larger_pixel_area(self):
        smaller = IconVariant("small.png", {480}, 96, 96)
        larger = IconVariant("large.png", {320}, 192, 192)

        self.assertGreater(variant_sort_key(larger), variant_sort_key(smaller))

    def test_fdroid_search_result_parsing(self):
        html = """
          <a class="package-header" href="https://f-droid.org/en/packages/org.example.app">
            <img class="package-icon" src="https://f-droid.org/repo/icon.png" />
            <div><h4 class="package-name"> Example App </h4>
              <span class="package-summary">A <b>useful</b> app</span></div>
          </a>
        """
        parser = FdroidSearchParser()
        parser.feed(html)

        self.assertEqual(len(parser.results), 1)
        self.assertEqual(parser.results[0].package, "org.example.app")
        self.assertEqual(parser.results[0].name, "Example App")
        self.assertEqual(parser.results[0].summary, "A useful app")
        self.assertEqual(parser.results[0].icon_url, "https://f-droid.org/repo/icon.png")


if __name__ == "__main__":
    unittest.main()
