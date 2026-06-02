grammar Pipeline;

// Parser rules
pipeline: (taskDef #singleTask | taskDef (taskDef #multipleTasks))* EOF;

taskDef: 'task' ID ('(' paramValue (',' paramValue)* ')')? '{'
          (paramsBlock | runBlock | successBlock | requiresBlock | outputStmt | timeoutStmt)*
          '}' ;

paramsBlock: 'params:' '{' (paramDef (',' paramDef)*)? '}' ;

paramDef: ID ':' type ('=' value)? (';'?);

type: 'string' | 'int' | 'float' | 'bool' | 'list[' ID ']' ;

value: STRING | INT | FLOAT | 'TRUE' | 'FALSE' | '[' (value (',' value)*)? ']' ;

runBlock: 'run:'? '{' (command (newLine command)*)? '}' ;

command: ANY_TEXT ;

successBlock: 'success:' '{' (successCriterion (newLine successCriterion)*)? '}' ;

successCriterion: ID ':' '{' (statement (newLine statement)*)? '}' ;

requiresBlock: 'requires:' '{' (statement (newLine statement)*)? '}' ;

outputStmt: 'output:' STRING ;

timeoutStmt: 'timeout:' FLOAT ;

// Expression statements
statement: varDecl #varDeclStmt
          | forLoop #forLoopStmt
          | ifBlock #ifBlockStmt
          | returnStmt #returnStmtStmt
          | functionCall #funcCallStmt
          ;

varDecl: type ID '=' expr ';'? ;

forLoop: 'for' '(' forInit ';' expr ';' forUpdate ')' '{' (statement (newLine statement)*)? '}' ;

forInit: type ID '=' expr
        | ; // empty for init

forUpdate: ID (incOp | assignExpr) ;

incOp: '++' | '--' ;

ifBlock: 'if' '(' expr ')' '{' (statement (newLine statement)*)? '}' ('elif' '(' expr ')' '{' (statement (newLine statement)*)? '}')* ('else' '{' (statement (newLine statement)*)? '}')? ;

returnStmt: 'return' expr ';'? ;

functionCall: ID '(' argList? ')' ';'? ;

argList: paramValue (',' paramValue)* ;

paramValue: expr | ID '=' expr ;

expr:
    expr '%' expr #concatExpr
    | expr ('==' | '!=' | '<' | '>' | '<=' | '>=') expr #compareExpr
    | expr ('+' | '-' | '*' | '/') expr #arithExpr
    | expr '&&' expr #andExpr
    | expr '||' expr #orExpr
    | '!' expr #notExpr
    | '(' expr ')' #parenExpr
    | ID '(' argList? ')' #funcCallExpr
    | ID '[' expr ']' #indexExpr
    | expr '++' | expr '--' #postIncExpr
    | ('+'|'-') expr #unaryExpr
    | ID #idExpr
    | (INT | FLOAT | STRING | 'TRUE' | 'FALSE') #literalExpr
    | 'for' #forExpr
    | 'while' '(' expr ')' '{' (statement (newLine statement)*)? '}' #whileExpr
    ;

// Lexer rules
ID: [a-zA-Z_][a-zA-Z0-9_]* ;
INT: [0-9]+ ;
FLOAT: [0-9]+ '.' [0-9]+ ;
STRING: '"' (~['\"] | '\\' .)* '"' | '\'' (~['\"] | '\\' .)* '\'' ;
COMMENT: '//' ~[\n]* -> skip ;
NEWLINE: [\n\r]+ -> skip ;
ANY_TEXT: ~[\n\r{']+? ;
WS: [ \t]+ -> skip ;
