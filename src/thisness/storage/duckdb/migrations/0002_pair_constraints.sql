-- Pair constraints (design 04 §2.5): reviewers' verdicts made durable.
-- Anchored on observations; written only by commit(); replayed from the ledger.
CREATE TABLE pair_constraints (
    constraint_id     VARCHAR PRIMARY KEY,
    kind              VARCHAR NOT NULL,     -- must_link | cannot_link
    left_id           VARCHAR NOT NULL,     -- observation; left_id < right_id
    right_id          VARCHAR NOT NULL,
    basis_decision_id VARCHAR NOT NULL,
    valid_from_seq    BIGINT NOT NULL,
    valid_to_seq      BIGINT
);
CREATE INDEX pair_constraints_left ON pair_constraints (left_id);
CREATE INDEX pair_constraints_right ON pair_constraints (right_id);
