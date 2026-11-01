"""Parser output for Objective-C sources."""

from __future__ import annotations

import unittest

from agent_gateway.services.graph.parsers import objc as objc_parser

SAMPLE = b"""#import <Foundation/Foundation.h>
#import "Local/Helper.h"

@interface Widget : NSObject
@property (nonatomic, strong) NSString *title;
@property (nonatomic, copy) void (^onTap)(void);
- (void)render:(NSString *)text;
+ (instancetype)widgetWithTitle:(NSString *)title;
@end

@implementation Widget
- (void)render:(NSString *)text {
    [self.title uppercaseString];
    [[Helper shared] format:text];
    [Helper alloc];
    [NSString stringWithFormat:@"%@", text];
    [text description];
}
@end

@interface Widget (Extra)
- (void)refresh;
@end
"""


class ObjCParserTest(unittest.TestCase):
    def test_extracts_classes_methods_and_properties(self):
        analysis = objc_parser.parse(SAMPLE, "src/Widget.m")
        symbols = {(item.name, item.kind, item.start_line) for item in analysis.symbols}
        self.assertIn(("Widget", "class", 4), symbols)
        self.assertIn(("Widget", "class", 11), symbols)
        self.assertIn(("Widget", "class", 21), symbols)
        self.assertIn(("render", "method", 7), symbols)
        self.assertIn(("render", "method", 12), symbols)
        self.assertIn(("widgetWithTitle", "method", 8), symbols)
        self.assertIn(("refresh", "method", 22), symbols)
        self.assertIn(("title", "property", 5), symbols)
        self.assertIn(("onTap", "property", 6), symbols)

        declaration = next(
            item
            for item in analysis.symbols
            if item.name == "render" and item.start_line == 7
        )
        self.assertEqual(declaration.end_line, 7)
        self.assertEqual(declaration.signature, "- (void)render:(NSString *)text;")

        definition = next(
            item
            for item in analysis.symbols
            if item.name == "render" and item.start_line == 12
        )
        self.assertEqual(definition.end_line, 18)
        self.assertEqual(definition.signature, "- (void)render:(NSString *)text {")

        interface = next(
            item
            for item in analysis.symbols
            if item.kind == "class" and item.start_line == 4
        )
        self.assertEqual(interface.end_line, 9)
        self.assertEqual(interface.signature, "@interface Widget : NSObject")

        property_symbol = next(
            item for item in analysis.symbols if item.name == "onTap"
        )
        self.assertEqual(
            property_symbol.signature,
            "@property (nonatomic, copy) void (^onTap)(void);",
        )

    def test_message_sends_use_root_receiver(self):
        source = b"""@interface Caller : NSObject
@end

@implementation Caller
- (void)run {
    [self.title uppercaseString];
    [[Helper shared] format:text];
    [[[Factory shared] manager] execute];
    [obj setTitle:title forKey:key];
}
@end
"""
        analysis = objc_parser.parse(source, "src/Caller.m")
        references = {
            (item.raw_name, item.kind, item.receiver, item.line)
            for item in analysis.references
        }
        self.assertIn(("uppercaseString", "call", "self", 6), references)
        self.assertIn(("format", "call", "Helper", 7), references)
        self.assertIn(("shared", "call", "Helper", 7), references)
        self.assertIn(("execute", "call", "Factory", 8), references)
        self.assertIn(("manager", "call", "Factory", 8), references)
        self.assertIn(("setTitle", "call", "obj", 9), references)

        execute = next(
            item for item in analysis.references if item.raw_name == "execute"
        )
        self.assertEqual(execute.from_name, "run")
        self.assertEqual(execute.from_line, 5)

    def test_alloc_and_new_emit_type_refs(self):
        source = b"""@implementation Maker
- (void)build {
    [Helper alloc];
    [Helper new];
    id helper = [Helper alloc];
}
@end
"""
        analysis = objc_parser.parse(source, "src/Maker.m")
        type_refs = [
            (item.raw_name, item.line)
            for item in analysis.references
            if item.kind == "type_ref"
        ]
        self.assertEqual(
            type_refs,
            [("Helper", 3), ("Helper", 4), ("Helper", 5)],
        )
        calls = [item.raw_name for item in analysis.references if item.kind == "call"]
        self.assertNotIn("alloc", calls)
        self.assertIn("new", calls)

    def test_imports_and_system_noise_are_excluded(self):
        source = b"""#import <Foundation/Foundation.h>
#import "Local/Helper.h"

@interface Quiet : NSObject
@end

@implementation Quiet
- (void)run:(id)obj {
    [NSNotificationCenter defaultCenter];
    [obj description];
    [obj copy];
    [obj performSelector:@selector(run)];
    // [obj hidden];
}
@end
"""
        analysis = objc_parser.parse(source, "src/Quiet.m")
        self.assertEqual(
            [(item.module, item.names, item.line) for item in analysis.imports],
            [
                ("Foundation/Foundation.h", (), 1),
                ("Local/Helper.h", (), 2),
            ],
        )
        references = [
            (item.raw_name, item.receiver, item.line) for item in analysis.references
        ]
        self.assertEqual(references, [("performSelector", "obj", 12)])

    def test_invalid_utf8_is_replaced(self):
        analysis = objc_parser.parse(
            b"@interface Broken \xff : NSObject\n@end\n", "src/Broken.m"
        )
        self.assertEqual(analysis.path, "src/Broken.m")
        self.assertEqual(analysis.lang, "objc")
        self.assertEqual(
            [(item.name, item.kind) for item in analysis.symbols], [("Broken", "class")]
        )


if __name__ == "__main__":
    unittest.main()
