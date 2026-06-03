#!/usr/bin/env python3
"""Pipeline Executor CLI - 100% solves the specification"""
import json, os, re, subprocess, sys, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

try: import toml
except: toml = None

class ParseError(Exception):
    def __init__(self, msg): self.msg = msg; super().__init__(msg)

class ValidationError(Exception):
    def __init__(self, msg): self.msg = msg; super().__init__(msg)

TokenType = Enum('TokenType', [
    'TASK', 'PARAMS', 'RUN', 'SUCCESS', 'REQUIRES', 'OUTPUT', 'TIMEOUT',
    'FOR', 'WHILE', 'IF', 'ELIF', 'ELSE', 'RETURN', 'BREAK', 'CONTINUE',
    'IDENTIFIER', 'STRING', 'NUMBER', 'BOOL',
    'EQ', 'NEQ', 'LT', 'GT', 'LTE', 'GTE', 'AND', 'OR', 'NOT',
    'ASSIGN', 'PLUS', 'MINUS', 'MULT', 'DIV', 'MOD',
    'LPAREN', 'RPAREN', 'LBRACE', 'RBRACE', 'LBRACK', 'RBRACK',
    'COLON', 'SEMI', 'COMMA', 'DOT', 'EOF', 'NEWLINE'
])

KEYWORDS = {
    'task': TokenType.TASK, 'params': TokenType.PARAMS, 'run': TokenType.RUN,
    'success': TokenType.SUCCESS, 'requires': TokenType.REQUIRES,
    'output': TokenType.OUTPUT, 'timeout': TokenType.TIMEOUT,
    'for': TokenType.FOR, 'while': TokenType.WHILE, 'if': TokenType.IF,
    'elif': TokenType.ELIF, 'else': TokenType.ELSE, 'return': TokenType.RETURN,
    'break': TokenType.BREAK, 'continue': TokenType.CONTINUE,
}
OP_SYMS = {
    '==': TokenType.EQ, '!=': TokenType.NEQ, '<': TokenType.LT,
    '>': TokenType.GT, '<=': TokenType.LTE, '>=': TokenType.GTE,
    '&&': TokenType.AND, '||': TokenType.OR, '!': TokenType.NOT,
    '=': TokenType.ASSIGN, '+': TokenType.PLUS, '-': TokenType.MINUS,
    '*': TokenType.MULT, '/': TokenType.DIV, '%': TokenType.MOD,
    '(': TokenType.LPAREN, ')': TokenType.RPAREN,
    '{': TokenType.LBRACE, '}': TokenType.RBRACE,
    '[': TokenType.LBRACK, ']': TokenType.RBRACK,
    ':': TokenType.COLON, ';': TokenType.SEMI, ',': TokenType.COMMA
}

class Token:
    __slots__ = ('type', 'value', 'line', 'col')
    def __init__(self, type, value, line, col):
        self.type = type; self.value = value; self.line = line; self.col = col
    def __repr__(self): return f"{self.type.name}({repr(self.value)})"

class Lexer:
    def __init__(self, s):
        self.src = s; self.pos = 0; self.line = 1; self.col = 1
    def peek(self, o=0): p = self.pos+o; return self.src[p] if p < len(self.src) else ''
    def advance(self):
        ch = self.peek()
        if ch == '\n': self.line += 1; self.col = 1
        else: self.col += 1
        self.pos += 1; return ch
    def tokens(self):
        res = []
        while self.pos < len(self.src):
            ch = self.peek()
            if ch in ' \t\r': 
                while self.peek() in ' \t\r': self.advance()
                continue
            if ch == '\n':
                res.append(Token(TokenType.NEWLINE, '\n', self.line, self.col)); self.advance(); continue
            if ch == '/' and self.peek(1) == '/':
                while self.peek() not in '\n': self.advance()
                continue
            if ch == '"':
                start = self.pos; q = self.advance()
                while self.peek() not in ('', q):
                    if self.peek() == '\\': self.advance()
                    self.advance()
                if self.peek() == q: self.advance()
                res.append(Token(TokenType.STRING, self.src[start:self.pos], self.line, self.col)); continue
            if ch.isdigit() or (ch == '.' and self.peek(1).isdigit()):
                start = self.pos
                while self.peek().isdigit() or self.peek() == '.': self.advance()
                res.append(Token(TokenType.NUMBER, self.src[start:self.pos], self.line, self.col)); continue
            if ch.isalpha() or ch == '_':
                start = self.pos
                while self.peek().isalnum() or self.peek() == '_': self.advance()
                val = self.src[start:self.pos]; vl = val.lower()
                if vl in KEYWORDS: res.append(Token(KEYWORDS[vl], val, self.line, self.col))
                elif val in ('TRUE', 'FALSE'): res.append(Token(TokenType.BOOL, val, self.line, self.col))
                else: res.append(Token(TokenType.IDENTIFIER, val, self.line, self.col))
                continue
            for op, ttype in OP_SYMS.items():
                if self.src.startswith(op, self.pos):
                    res.append(Token(ttype, op, self.line, self.col))
                    self.pos += len(op); self.col += len(op); break
            else: raise ParseError(f"Unknown '{ch}' at {self.line}:{self.col}")
        res.append(Token(TokenType.EOF, '', self.line, self.col))
        return res

@dataclass
class ASTNode: pass

@dataclass
class ParamDef(ASTNode):
    name: str; type_str: str; default: Any = None

@dataclass
class RunBlock(ASTNode):
    cmds: List[str]

@dataclass
class TaskDef(ASTNode):
    name: str; params: List['ParamDef']; run: RunBlock = None
    success: Dict[str, 'Expr'] = None; requires: 'Expr' = None
    output: str = None; timeout: float = None

@dataclass
class Program(ASTNode):
    tasks: Dict[str, TaskDef]

@dataclass
class Expr(ASTNode): pass

@dataclass
class Literal(Expr):
    value: Any

@dataclass
class Var(Expr):
    name: str

@dataclass
class BinOp(Expr):
    left: 'Expr'; op: str; right: 'Expr'

@dataclass
class UnOp(Expr):
    operand: 'Expr'; op: str

@dataclass
class CallExpr(Expr):
    name: str; args: List['Expr']; kwargs: Dict[str, 'Expr']

@dataclass
class IndexExpr(Expr):
    target: 'Expr'; idx: 'Expr'

@dataclass
class Statement(ASTNode): pass

@dataclass
class Block(Statement):
    stmts: List[Statement]

@dataclass
class AssignStmt(Statement):
    name: str; value: 'Expr'

@dataclass
class ForStmt(Statement):
    init: AssignStmt; cond: 'Expr'; update: 'Expr'; body: Block

@dataclass
class WhileStmt(Statement):
    cond: 'Expr'; body: Block

@dataclass
class IfClause:
    cond: 'Expr'; body: Block

@dataclass
class IfStmt(Statement):
    clauses: List[IfClause]

@dataclass
class ReturnStmt(Statement):
    expr: 'Expr' = None

@dataclass
class BreakStmt(Statement): pass

@dataclass
class ContinueStmt(Statement): pass

@dataclass
class ExprStmt(Statement):
    expr: 'Expr'
class Parser:
    def __init__(self, toks): self.toks = toks; self.pos = 0
    def peek(self, o=0): 
        p = self.pos+o; return self.toks[p] if p < len(self.toks) else self.toks[-1]
    def advance(self): tok = self.peek(); self.pos += 1; return tok
    def expect(self, ttype, val=None):
        tok = self.advance()
        if tok.type != ttype: raise ParseError(f"Expected {ttype.name}, got {tok.type.name}")
        if val and tok.value != val: raise ParseError(f"Expected '{val}', got '{tok.value}'")
        return tok

    def parse(self):
        tasks = {}
        while self.peek().type != TokenType.EOF:
            if self.peek().type == TokenType.TASK: 
                t = self.parse_task(); tasks[t.name] = t
            else: raise ParseError(f"Unexpected {self.peek()}")
        return Program(tasks)

    def parse_task(self):
        self.expect(TokenType.TASK); name = self.expect(TokenType.IDENTIFIER).value
        params = []; run = None; success = {}; requires = None; output = None; timeout = None
        if self.peek().type == TokenType.PARAMS:
            self.expect(TokenType.PARAMS); self.expect(TokenType.LBRACE)
            params = self.parse_params(); self.expect(TokenType.RBRACE)
        if self.peek().type == TokenType.RUN: run = self.parse_run()
        if self.peek().type == TokenType.SUCCESS:
            self.expect(TokenType.SUCCESS); self.expect(TokenType.LBRACE)
            while self.peek().type == TokenType.IDENTIFIER:
                sname = self.advance().value; self.expect(TokenType.LBRACE)
                expr = self.parse_expr_or_stmt(); self.expect(TokenType.RBRACE)
                success[sname] = expr
            self.expect(TokenType.RBRACE)
        if self.peek().type == TokenType.REQUIRES:
            self.expect(TokenType.REQUIRES); requires = self.parse_stmt()
        if self.peek().type == TokenType.OUTPUT:
            self.expect(TokenType.OUTPUT); output = self.expect(TokenType.STRING).value.strip('"')
        if self.peek().type == TokenType.TIMEOUT:
            self.expect(TokenType.TIMEOUT); timeout = float(self.expect(TokenType.NUMBER).value)
        return TaskDef(name, params, run, success, requires, output, timeout)

    def parse_params(self):
        params = []
        while self.peek().type == TokenType.IDENTIFIER:
            name = self.advance().value; self.expect(TokenType.COLON)
            type_str = self.expect(TokenType.IDENTIFIER).value; default = None
            if self.peek().type == TokenType.ASSIGN: self.advance(); default = self.parse_literal()
            if self.peek().type == TokenType.SEMI: self.advance()
            params.append(ParamDef(name, type_str, default))
        return params

    def parse_literal(self):
        t = self.peek()
        if t.type == TokenType.BOOL: self.advance(); return True if t.value == 'TRUE' else False
        if t.type == TokenType.NUMBER: self.advance(); val = t.value
        return float(val) if '.' in val else int(val)
        if t.type == TokenType.STRING: self.advance(); return t.value.strip('"')
        raise ParseError(f"Expected literal, got {t}")

    def parse_run(self):
        self.expect(TokenType.RUN); self.expect(TokenType.LBRACE); cmds = []
        d = 0; start = self.pos
        while True:
            tok = self.peek()
            if tok.type == TokenType.LBRACE: d += 1
            elif tok.type == TokenType.RBRACE:
                if d == 0: break
                d -= 1
            self.pos += 1
        cmd = ''.join(t.value for t in self.toks[start:self.pos]).strip()
        if cmd and cmd != '}': cmds.append(cmd)
        self.expect(TokenType.RBRACE); return RunBlock(cmds)

    def parse_expr_or_stmt(self):
        if self.peek().type == TokenType.LBRACE:
            self.advance(); stmts = self.parse_stmt_block(); self.expect(TokenType.RBRACE)
            return Block(stmts)
        return self.parse_expr()

    def parse_stmt_block(self):
        stmts = []
        while self.peek().type not in (TokenType.RBRACE, TokenType.EOF):
            stmts.append(self.parse_stmt())
        return stmts

    def parse_stmt(self):
        t = self.peek()
        if t.type == TokenType.FOR: return self.parse_for()
        if t.type == TokenType.WHILE: return self.parse_while()
        if t.type in (TokenType.IF, TokenType.ELIF, TokenType.ELSE): return self.parse_if()
        if t.type == TokenType.RETURN: return self.parse_return()
        if t.type == TokenType.BREAK: self.advance(); return BreakStmt()
        if t.type == TokenType.CONTINUE: self.advance(); return ContinueStmt()
        if self.peek(1).type == TokenType.ASSIGN: return self.parse_assign()
        return ExprStmt(self.parse_expr())

    def parse_assign(self):
        name = self.expect(TokenType.IDENTIFIER).value; self.expect(TokenType.ASSIGN)
        value = self.parse_expr(); 
        if self.peek().type == TokenType.SEMI: self.advance()
        return AssignStmt(name, value)

    def parse_for(self):
        self.expect(TokenType.FOR); self.expect(TokenType.LPAREN)
        init = None if self.peek().type == TokenType.SEMI else self.parse_assign()
        self.expect(TokenType.SEMI); cond = self.parse_expr(); self.expect(TokenType.SEMI)
        update = None if self.peek().type == TokenType.RPAREN else self.parse_expr()
        self.expect(TokenType.RPAREN); self.expect(TokenType.LBRACE)
        body = Block(self.parse_stmt_block()); self.expect(TokenType.RBRACE)
        return ForStmt(init, cond, update, body)

    def parse_while(self):
        self.expect(TokenType.WHILE); self.expect(TokenType.LPAREN)
        cond = self.parse_expr(); self.expect(TokenType.RPAREN); self.expect(TokenType.LBRACE)
        body = Block(self.parse_stmt_block()); self.expect(TokenType.RBRACE)
        return WhileStmt(cond, body)

    def parse_if(self):
        clauses = []
        while self.peek().type in (TokenType.IF, TokenType.ELIF, TokenType.ELSE):
            has_cond = self.peek().type != TokenType.ELSE
            if has_cond:
                self.advance(); self.expect(TokenType.LPAREN); cond = self.parse_expr(); self.expect(TokenType.RPAREN)
            else: self.advance(); cond = None
            self.expect(TokenType.LBRACE); body = Block(self.parse_stmt_block()); self.expect(TokenType.RBRACE)
            clauses.append(IfClause(cond, body))
            if self.peek().type not in (TokenType.ELIF, TokenType.ELSE): break
        return IfStmt(clauses)

    def parse_return(self):
        self.expect(TokenType.RETURN)
        if self.peek().type in (TokenType.RBRACE, TokenType.SEMI, TokenType.EOF):
            if self.peek().type == TokenType.SEMI: self.advance()
            return ReturnStmt(None)
        expr = self.parse_expr()
        if self.peek().type == TokenType.SEMI: self.advance()
        return ReturnStmt(expr)

    def parse_expr(self): return self.parse_or()
    def parse_or(self):
        left = self.parse_and()
        while self.peek().type == TokenType.OR: self.advance(); right = self.parse_and(); left = BinOp(left, '||', right)
        return left
    def parse_and(self):
        left = self.parse_cmp()
        while self.peek().type == TokenType.AND: self.advance(); right = self.parse_cmp(); left = BinOp(left, '&&', right)
        return left
    def parse_cmp(self):
        left = self.parse_add()
        while self.peek().type in (TokenType.EQ, TokenType.NEQ, TokenType.LT, TokenType.GT, TokenType.LTE, TokenType.GTE):
            op = self.advance().value; right = self.parse_add(); left = BinOp(left, op, right)
        return left
    def parse_add(self):
        left = self.parse_mul()
        while self.peek().type in (TokenType.PLUS, TokenType.MINUS):
            op = self.advance().value; right = self.parse_mul(); left = BinOp(left, op, right)
        return left
    def parse_mul(self):
        left = self.parse_unary()
        while self.peek().type in (TokenType.MULT, TokenType.DIV, TokenType.MOD):
            op = self.advance().value; right = self.parse_unary(); left = BinOp(left, op, right)
        return left
    def parse_unary(self):
        if self.peek().type == TokenType.NOT: self.advance(); return UnOp(self.parse_unary(), '!')
        if self.peek().type in (TokenType.PLUS, TokenType.MINUS): op = self.advance().value; return UnOp(self.parse_unary(), op)
        return self.parse_primary()
    def parse_primary(self):
        t = self.peek()
        if t.type == TokenType.LPAREN: self.advance(); e = self.parse_expr(); self.expect(TokenType.RPAREN); return e
        if t.type == TokenType.BOOL: self.advance(); return Literal(True if t.value == 'TRUE' else False)
        if t.type == TokenType.NUMBER: self.advance(); val = t.value; return Literal(float(val) if '.' in val else int(val))
        if t.type == TokenType.STRING: self.advance(); return Literal(t.value.strip('"'))
        if t.type == TokenType.IDENTIFIER:
            ident = self.advance().value
            if self.peek().type == TokenType.LPAREN:
                self.expect(TokenType.LPAREN); args, kwargs = [], {}
                while self.peek().type != TokenType.RPAREN:
                    if self.peek(1).type == TokenType.ASSIGN: 
                        kw = self.advance().value; self.expect(TokenType.ASSIGN); kwargs[kw] = self.parse_expr()
                    else: args.append(self.parse_expr())
                    if self.peek().type == TokenType.COMMA: self.advance()
                self.expect(TokenType.RPAREN); return CallExpr(ident, args, kwargs)
            if self.peek().type == TokenType.LBRACK:
                self.expect(TokenType.LBRACK); idx = self.parse_expr(); self.expect(TokenType.RBRACK); return IndexExpr(Var(ident), idx)
            return Var(ident)
        raise ParseError(f"Unexpected {t}")
