//! Legalize instructions.
//!
//! A legal instruction is one that can be mapped directly to a machine code instruction for the
//! target ISA. The `legalize_function()` function takes as input any function and transforms it
//! into an equivalent function using only legal instructions.
//!
//! The characteristics of legal instructions depend on the target ISA, so any given instruction
//! can be legal for one ISA and illegal for another.
//!
//! Besides transforming instructions, the legalizer also fills out the `function.encodings` map
//! which provides a legal encoding recipe for every instruction.
//!
//! The legalizer does not deal with register allocation constraints. These constraints are derived
//! from the encoding recipes, and solved later by the register allocator.

use ir::Function;
use isa::TargetIsa;

/// Legalize `func` for `isa`.
///
/// - Transform any instructions that don't have a legal representation in `isa`.
/// - Fill out `func.encodings`.
///
pub fn legalize_function(func: &mut Function, isa: &TargetIsa) {
    // TODO: This is very simplified and incomplete.
    func.encodings.resize(func.dfg.num_insts());
    for ebb in func.layout.ebbs() {
        for inst in func.layout.ebb_insts(ebb) {
            match isa.encode(&func.dfg, &func.dfg[inst]) {
                Some(encoding) => func.encodings[inst] = encoding,
                None => {
                    // TODO: We should transform the instruction into legal equivalents.
                    // Possible strategies are:
                    // 1. Expand instruction into sequence of legal instructions. Possibly
                    //    iteratively.
                    // 2. Split the controlling type variable into high and low parts. This applies
                    //    both to SIMD vector types which can be halved and to integer types such
                    //    as `i64` used on a 32-bit ISA.
                    // 3. Promote the controlling type variable to a larger type. This typically
                    //    means expressing `i8` and `i16` arithmetic in terms if `i32` operations
                    //    on RISC targets. (It may or may not be beneficial to promote small vector
                    //    types versus splitting them.)
                    // 4. Convert to library calls. For example, floating point operations on an
                    //    ISA with no IEEE 754 support.
                    //
                    // The iteration scheme used here is not going to cut it. Transforming
                    // instructions involves changing `function.layout` which is impossiblr while
                    // it is referenced by the two iterators. We need a layout cursor that can
                    // maintain a position *and* permit inserting and replacing instructions.
                }
            }
        }
    }
}
