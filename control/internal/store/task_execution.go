package store

import (
	"context"
	"fmt"
)

func (s *SQLite) FailTask(ctx context.Context, id, code, message string) error {
	return s.finishTask(ctx, id, "FAILED", code, message)
}

func (s *SQLite) finishTask(ctx context.Context, id, state, code, message string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := timestamp()
	// Cancellation can win a race with completion. Terminal tasks cannot be
	// overwritten by delayed Worker responses or dispatcher failure handling.
	res, err := tx.ExecContext(ctx, "UPDATE tasks SET state=?,error_code=?,result=?,updated_at=? WHERE id=? AND state IN ('QUEUED','RUNNING')", state, code, message, now, id)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrConflict
	}
	_, err = tx.ExecContext(ctx, `UPDATE agents SET state='IDLE',updated_at=?,last_active_at=?
WHERE id=(SELECT agent_id FROM tasks WHERE id=?) AND state='ACTIVE'
AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.agent_id=agents.id AND tasks.state='RUNNING')`, now, now, id)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// InterruptRunningTasks is a single-owner startup reconciliation. Never replay a
// command with unknown side effects. Tracked Workers must be destroyed first.
func (s *SQLite) InterruptRunningTasks(ctx context.Context) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var pending int
	if err = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM task_workers").Scan(&pending); err != nil {
		return err
	}
	if pending != 0 {
		return fmt.Errorf("cannot reconcile tasks with pending Worker cleanup")
	}
	now := timestamp()
	if _, err = tx.ExecContext(ctx, "UPDATE tasks SET state='FAILED',error_code='execution_interrupted',result='Control Plane restarted; previous outcome is unknown. Task was not replayed.',updated_at=? WHERE state='RUNNING'", now); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "UPDATE agents SET state='IDLE',updated_at=? WHERE state='ACTIVE' AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.agent_id=agents.id AND tasks.state='RUNNING')", now); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *SQLite) TrackTaskWorker(ctx context.Context, taskID, containerID string) error {
	if containerID == "" {
		return fmt.Errorf("empty Worker container ID")
	}
	res, err := s.db.ExecContext(ctx, "INSERT INTO task_workers(task_id,container_id) SELECT id,? FROM tasks WHERE id=? AND state='RUNNING'", containerID, taskID)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrConflict
	}
	return nil
}

func (s *SQLite) ListTaskWorkers(ctx context.Context) (map[string]string, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT task_id,container_id FROM task_workers ORDER BY task_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	workers := make(map[string]string)
	for rows.Next() {
		var taskID, containerID string
		if err := rows.Scan(&taskID, &containerID); err != nil {
			return nil, err
		}
		workers[taskID] = containerID
	}
	return workers, rows.Err()
}

func (s *SQLite) ClearTaskWorker(ctx context.Context, taskID string) error {
	_, err := s.db.ExecContext(ctx, "DELETE FROM task_workers WHERE task_id=?", taskID)
	return err
}
