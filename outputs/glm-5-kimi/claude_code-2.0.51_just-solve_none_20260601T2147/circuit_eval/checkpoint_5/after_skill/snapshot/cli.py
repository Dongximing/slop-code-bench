"""CLI entry point for circopt."""

import sys
import json

from errors import CircError, EvalError, get_exit_code
from ast_nodes import Circuit
from trivalue import TriValue, format_trivalue, format_value
from parser import CircParser, parse_json_circuit, parse_bench_circuit, parse_3val_input
from validator import Validator
from evaluator import Evaluator

VERSION = "1.0.0"


def _output_json(data: dict):
    print(json.dumps(data, separators=(',', ':')))


def _signal_list(signals):
    return [{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in sorted(signals, key=lambda s: s.name)]


def _output_error(error, command: str, json_mode: bool) -> int:
    exit_code = get_exit_code(error.error_type)
    if json_mode:
        _output_json({"ok": False, "command": command, "exit_code": exit_code, "error": error.to_dict()})
    else:
        loc = ""
        if hasattr(error, 'file') and error.file:
            loc = f"{error.file}:"
            if error.line:
                loc += f"{error.line}:"
                if error.col:
                    loc += f"{error.col}:"
            loc += " "
        print(f"{loc}Error: {error.message}", file=sys.stderr)
    return exit_code


def _detect_format(filename: str) -> str:
    if filename.endswith('.circ'):
        return 'circ'
    if filename.endswith('.json'):
        return 'json'
    if filename.endswith('.bench'):
        return 'bench'
    return None


def load_circuit(filename: str, command: str, json_mode: bool, format_override: str = None):
    try:
        with open(filename, 'r') as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        return _output_error(CircError("FileNotFoundError", f"Cannot read file: {filename}", filename), command, json_mode)

    if format_override and format_override != 'auto':
        format_name = format_override
    else:
        format_name = _detect_format(filename)
        if format_name is None:
            return _output_error(CircError("UnknownInputFormatError", f"Cannot determine format from extension: {filename}"), command, json_mode)

    parsers = {
        'circ': lambda: CircParser(content, filename).parse(),
        'json': lambda: parse_json_circuit(content, filename),
        'bench': lambda: parse_bench_circuit(content, filename),
    }

    if format_name not in parsers:
        return _output_error(CircError("UnknownInputFormatError", f"Unknown format: {format_name}"), command, json_mode)

    try:
        circuit = parsers[format_name]()
        Validator(circuit, filename).validate()
    except CircError as e:
        return _output_error(e, command, json_mode)

    return circuit, format_name


def check_command(args: list, json_mode: bool, format_override: str = None) -> int:
    if not args:
        return _output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)
    filename = args[0]
    result = load_circuit(filename, "check", json_mode, format_override)
    if isinstance(result, int):
        return result
    circuit, format_name = result
    inputs = _signal_list(circuit.inputs)
    outputs = _signal_list(circuit.outputs)
    if json_mode:
        _output_json({"ok": True, "command": "check", "format": format_name, "inputs": inputs, "outputs": outputs})
    else:
        print("Circuit is valid.")
        print(f"Inputs: {', '.join(s['name'] for s in inputs)}")
        print(f"Outputs: {', '.join(s['name'] for s in outputs)}")
    return 0


def _parse_eval_args(args, json_mode):
    if not args:
        return None, _output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)
    opts = {
        'filename': args[0],
        'set_values': {},
        'default_value': None,
        'allow_extra': False,
        'radix': 'bin',
        'mode': '2val',
        'format': 'auto',
    }

    def _opt_val(i, flag_name, valid_vals, err_label):
        if i + 1 >= len(args):
            return None, _output_error(CircError("CliUsageError", f"{flag_name} requires a value ({err_label})"), "eval", json_mode), i
        val = args[i + 1].lower()
        if valid_vals and val not in valid_vals:
            return None, _output_error(CircError("CliUsageError", f"Invalid {flag_name} value: {val}, must be {err_label}"), "eval", json_mode), i
        return val, None, i

    i = 1
    while i < len(args):
        arg = args[i]
        if arg == '--set':
            if i + 1 >= len(args):
                return None, _output_error(CircError("CliUsageError", "--set requires a name=value argument"), "eval", json_mode)
            set_arg = args[i + 1]
            if '=' not in set_arg:
                return None, _output_error(CircError("CliUsageError", f"Invalid --set format: {set_arg}, expected name=value"), "eval", json_mode)
            name, value = set_arg.split('=', 1)
            opts['set_values'][name] = value
            i += 2
        elif arg == '--default':
            val, err, i = _opt_val(i, "--default", ('0', '1'), "0 or 1")
            if err:
                return None, err
            opts['default_value'] = int(val)
            i += 2
        elif arg == '--allow-extra':
            opts['allow_extra'] = True
            i += 1
        elif arg == '--radix':
            val, err, i = _opt_val(i, "--radix", ('bin', 'hex', 'dec'), "bin, hex, or dec")
            if err:
                return None, err
            opts['radix'] = val
            i += 2
        elif arg == '--mode':
            val, err, i = _opt_val(i, "--mode", ('2val', '3val'), "2val or 3val")
            if err:
                return None, err
            opts['mode'] = val
            i += 2
        elif arg == '--format':
            val, err, i = _opt_val(i, "--format", ('auto', 'circ', 'json', 'bench'), "auto, circ, json, or bench")
            if err:
                return None, err
            opts['format'] = val
            i += 2
        elif '=' in arg:
            name, value = arg.split('=', 1)
            opts['set_values'][name] = value
            i += 1
        else:
            return None, _output_error(CircError("CliUsageError", f"Unknown option: {arg}"), "eval", json_mode)
    return opts, None


def _default_fill(default_value: int, width: int) -> TriValue:
    if width == 1:
        return TriValue.from_int(default_value, 1)
    fill = default_value * ((1 << width) - 1) if default_value else 0
    return TriValue.from_int(fill, width)


def _format_outputs(circuit: Circuit, results: dict, mode: str, radix: str, json_mode: bool):
    sorted_outputs = sorted(results.items(), key=lambda x: x[0])

    def _fmt_val(name, val):
        sig = circuit.get_signal(name)
        w = sig.width if sig else 1
        if mode == '2val':
            return format_value(val.to_int(), w, radix)
        return format_trivalue(val)

    def _sig_info(name):
        sig = circuit.get_signal(name)
        return {
            "name": name,
            "msb": sig.msb if sig else 0,
            "lsb": sig.lsb if sig else 0,
        }

    if json_mode:
        output_list = [{**_sig_info(name), "value": _fmt_val(name, val)} for name, val in sorted_outputs]
        response = {
            "ok": True, "command": "eval", "mode": mode,
            "radix": radix if mode == '2val' else 'bin',
            "inputs": _signal_list(circuit.inputs),
            "outputs": output_list,
        }
        _output_json(response)
    else:
        for name, val in sorted_outputs:
            print(f"{name}={_fmt_val(name, val)}")


def eval_command(args: list, json_mode: bool, format_override: str = 'auto') -> int:
    opts, err = _parse_eval_args(args, json_mode)
    if err is not None:
        return err

    filename = opts['filename']
    set_values = opts['set_values']
    default_value = opts['default_value']
    allow_extra = opts['allow_extra']
    radix = opts['radix']
    mode = opts['mode']
    format_val = opts['format'] if opts['format'] != 'auto' else format_override

    if mode == '3val' and radix != 'bin':
        return _output_error(EvalError("RadixNotAllowedIn3ValError", "Only --radix bin is allowed with --mode 3val"), "eval", json_mode)

    result = load_circuit(filename, "eval", json_mode, format_val)
    if isinstance(result, int):
        return result
    circuit, _ = result
    input_names = {sig.name for sig in circuit.inputs}

    for name in set_values:
        if name not in input_names and not allow_extra:
            return _output_error(EvalError("UnknownInputError", f"Unknown input: {name}"), "eval", json_mode)

    missing_inputs = input_names - set(set_values.keys())
    if missing_inputs and default_value is None:
        return _output_error(EvalError("MissingInputError", f"Missing input values for: {', '.join(sorted(missing_inputs))}"), "eval", json_mode)

    inputs = {}
    try:
        for sig in circuit.inputs:
            if sig.name in set_values:
                inputs[sig.name] = parse_3val_input(set_values[sig.name], sig.width, sig.name, filename)
            elif default_value is not None:
                inputs[sig.name] = _default_fill(default_value, sig.width)
    except EvalError as e:
        return _output_error(e, "eval", json_mode)

    try:
        results = Evaluator(circuit).evaluate(inputs)
    except Exception as e:
        return _output_error(CircError("InternalError", str(e)), "eval", json_mode)

    _format_outputs(circuit, results, mode, radix, json_mode)
    return 0


def _parse_global_format(args):
    """Extract --format from args, returning (format_value, remaining_args, error_result)."""
    i = 0
    while i < len(args):
        if args[i] == '--format':
            if i + 1 >= len(args):
                return None, None, "--format requires a value (auto, circ, json, or bench)"
            val = args[i + 1].lower()
            if val not in ('auto', 'circ', 'json', 'bench'):
                return None, None, f"Invalid --format value: {val}, must be auto, circ, json, or bench"
            return val, args[:i] + args[i+2:], None
        i += 1
    return 'auto', args, None


def main():
    args = sys.argv[1:]
    if '--help' in args:
        print("""Usage: circopt.py [OPTIONS] <COMMAND> [ARGS]

Options:
  --help     Show this help message and exit
  --version  Show version and exit
  --json     Output in JSON format
  --format   Input format: auto, circ, json, bench (default: auto)

Commands:
  check      Validate a circuit file
  eval       Evaluate a circuit with given inputs

Run 'circopt.py <COMMAND> --help' for more information on a command.""")
        return 0
    if '--version' in args:
        json_mode = '--json' in args
        if json_mode:
            _output_json({"ok": True, "command": "__version__", "version": VERSION})
        else:
            print(VERSION)
        return 0

    json_mode = '--json' in args
    args = [a for a in args if a != '--json']

    format_override, args, fmt_err = _parse_global_format(args)
    if fmt_err:
        return _output_error(CircError("CliUsageError", fmt_err), "__cli__", json_mode)

    if not args:
        return _output_error(CircError("CliUsageError", "No command provided. Use --help for usage information."), "__cli__", json_mode)

    command, *command_args = args
    if command == 'check':
        return check_command(command_args, json_mode, format_override)
    if command == 'eval':
        return eval_command(command_args, json_mode, format_override)
    return _output_error(CircError("CliUsageError", f"Unknown command: {command}"), "__cli__", json_mode)


if __name__ == '__main__':
    sys.exit(main())
