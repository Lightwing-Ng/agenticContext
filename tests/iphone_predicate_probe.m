#import <Foundation/Foundation.h>

// Exercise Apple's actual predicate parser, rather than implementing a second
// parser in the Python fake. Input and output contain synthetic test data only.
int main(void) {
    @autoreleasepool {
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSArray *cases = [NSJSONSerialization JSONObjectWithData:input options:0 error:nil];
        if (![cases isKindOfClass:[NSArray class]]) return 2;
        NSMutableArray *results = [NSMutableArray array];
        for (NSDictionary *item in cases) {
            @try {
                NSPredicate *query = [NSPredicate predicateWithFormat:item[@"predicate"]];
                [results addObject:@{@"parsed": @YES, @"matched": @([query evaluateWithObject:item[@"object"]])}];
            } @catch (NSException *exception) {
                [results addObject:@{@"parsed": @NO, @"error": exception.reason ?: @"predicate exception"}];
            }
        }
        NSData *output = [NSJSONSerialization dataWithJSONObject:results options:0 error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
    }
    return 0;
}
