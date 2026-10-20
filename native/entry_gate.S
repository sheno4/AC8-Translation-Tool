/* Preserve the raw PE entry context, initialize outside the loader lock, and tail-jump back. */
.intel_syntax noprefix
.text
.p2align 4
.globl AC8EntryGate
.def AC8EntryGate; .scl 2; .type 32; .endef
AC8EntryGate:
    pushfq
    push rax
    push rcx
    push rdx
    push rbx
    push rbp
    push rsi
    push rdi
    push r8
    push r9
    push r10
    push r11
    push r12
    push r13
    push r14
    push r15
    mov rbx, rsp
    and rsp, -16
    sub rsp, 0x80
    /* 32-byte shadow space followed by the six Win64 volatile XMM registers. */
    movdqu XMMWORD PTR [rsp+0x20], xmm0
    movdqu XMMWORD PTR [rsp+0x30], xmm1
    movdqu XMMWORD PTR [rsp+0x40], xmm2
    movdqu XMMWORD PTR [rsp+0x50], xmm3
    movdqu XMMWORD PTR [rsp+0x60], xmm4
    movdqu XMMWORD PTR [rsp+0x70], xmm5
    cld
    call AC8RestoreAndInitialize
    movdqu xmm0, XMMWORD PTR [rsp+0x20]
    movdqu xmm1, XMMWORD PTR [rsp+0x30]
    movdqu xmm2, XMMWORD PTR [rsp+0x40]
    movdqu xmm3, XMMWORD PTR [rsp+0x50]
    movdqu xmm4, XMMWORD PTR [rsp+0x60]
    movdqu xmm5, XMMWORD PTR [rsp+0x70]
    mov rsp, rbx
    pop r15
    pop r14
    pop r13
    pop r12
    pop r11
    pop r10
    pop r9
    pop r8
    pop rdi
    pop rsi
    pop rbp
    pop rbx
    pop rdx
    pop rcx
    pop rax
    popfq
    jmp QWORD PTR [rip+AC8OriginalEntry]
